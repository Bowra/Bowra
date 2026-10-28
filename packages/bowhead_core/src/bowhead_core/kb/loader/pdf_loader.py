"""PDF loader (pypdfium2 L0 + qwen3-vl-plus L2 page-by-page).

Tech design v3.6 §3.1 + image-extraction sprint v1.0 §2.3 + image-slim sprint.

V1: marker-pdf L0 (deprecated — 拉 torch+surya+transformers ~3GB).
V2 (image-extraction sprint): scan-only PDF → L2 vision LLM (qwen3-vl-plus).
V3 (image-slim sprint): 砍 marker, L0 改 pypdfium2 简单文本提取. 简单 native PDF (合同/
回单/简历) 文字够用; 复杂 multi-column / 表格密集 / scan-only PDF 自动走 L2 vision.
省 ~3GB image (torch+surya+transformers 全砍).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING

from bowhead_core.kb.document import KbDocument
from bowhead_core.kb.loader.base import BaseLoader, LoadResult

if TYPE_CHECKING:
    from bowhead_core.kb.vision_llm import VisionLLMClient

logger = logging.getLogger(__name__)

# PDFium is a global singleton, **not thread-safe** (official pypdfium2: callers must serialize concurrent use).
# All three sync helpers run in a thread pool via asyncio.to_thread—vision L2's
# Semaphore(VISION_CONCURRENCY=4) once let _render_one_page enter PDFium from 4 threads →
# races corrupted library state; **all later PdfDocument opens in that process reported "Data format error"**
# (2026-07-13 local reload evidence: first few PDFs per process succeeded, then 72/77 consecutive failures; S3 bytes intact;
# suspected real cause of the 62% placeholder corpus incident—one batch ingest poisoned the persistent gateway,
# then all later scans silently degraded to placeholders). Fix: hold a global lock for all pypdfium2 calls; render
# serially, while the main cost (vision LLM network calls) stays concurrent outside the lock, with little throughput loss.
_PDFIUM_LOCK = threading.Lock()


# [P1-T2] vision LLM model env-configurable, default qwen3-vl-plus (Sprint 0 spike 4 verified)
VISION_LLM_MODEL = os.environ.get("BOWHEAD_KB_VISION_LLM_MODEL", "qwen3-vl-plus")

# Scan-only detection thresholds
MIN_TEXT_CHARS_FOR_NATIVE = 50
"""L0 输出 < 50 char 视为扫描件 → fallback L2 vision."""

# matches: ![alt](_page_N_Figure_M.jpeg) — legacy marker output, still detect for backward compat
_FIGURE_MD_RE = re.compile(r"!\[[^\]]*\]\(_page_\d+_[A-Za-z]+_\d+\.\w+\)")

# vision LLM render scale (2.0 = roughly 144 DPI which is enough for OCR but small payload)
PDF_RENDER_SCALE = 2.0

# Cost guard: max pages sent to vision LLM. V1 cap=20 truncated real B2B contracts/financial reports
# (often >20 pages); 0.7.5 raised it to 100. Still reducible via env.
VISION_PAGE_CAP = int(os.environ.get("BOWHEAD_KB_VISION_PAGE_CAP", "100"))

# Bounded concurrency: 100 pages serial ~5min, 4 concurrent ~1.5min, without significant rate-limit risk increase.
VISION_CONCURRENCY = int(os.environ.get("BOWHEAD_KB_VISION_CONCURRENCY", "4"))


def _is_garbled(text: str) -> bool:
    """Heuristic: text 含 >50% non-printable / 字体 cmap 错 → 乱码 (扫描件 PDF)."""
    if not text:
        return False
    non_printable = sum(1 for c in text if not c.isprintable() and c not in "\n\t ")
    return non_printable / len(text) > 0.5


def _is_scan_only(text: str) -> bool:
    """[image-extraction] marker 输出仅 figure md 占位 → 真实是扫描 PDF.

    检测规则:
    - 移掉 figure md 占位 + 空白后 char count < MIN_TEXT_CHARS_FOR_NATIVE
    - OR 整文档全是 figure md (>= 1 figure, 0 non-figure text)
    """
    if not text:
        return True
    cleaned = _FIGURE_MD_RE.sub("", text).strip()
    return len(cleaned) < MIN_TEXT_CHARS_FOR_NATIVE


def _md_to_documents(md_text: str, page_count: int) -> list[KbDocument]:
    """marker / vision LLM 输 markdown → KbDocument list."""
    return [
        KbDocument(
            page_content=md_text,
            metadata={"page_count": page_count, "page": None},
        )
    ]


class PdfLoader(BaseLoader):
    """PDF loader: pypdfium2 L0 default, scan-only fallback → vision LLM L2.

    image-slim sprint: 弃 marker-pdf (~3GB torch+surya+transformers), L0 改 pypdfium2
    简单文本提取. 复杂 PDF (multi-column / 表格密集 / scan-only) 自动走 L2 vision LLM.
    """

    def __init__(self, vision_client: VisionLLMClient | None = None) -> None:
        self._vision = vision_client

    async def load(self, file_path: str, *, use_llm: bool = False) -> LoadResult:
        t0 = time.time()

        # ── L0: pypdfium2 text extract ──
        l0_text, l0_page_count, l0_failed = await self._pypdfium_l0(file_path)

        # If L0 produced usable text (not garbled, not scan-only), return as L0.
        if l0_text and not _is_garbled(l0_text) and not _is_scan_only(l0_text):
            return LoadResult(
                documents=_md_to_documents(l0_text, l0_page_count),
                layer_used="L0",
                duration_ms=int((time.time() - t0) * 1000),
                metadata={
                    "page_count": l0_page_count,
                    "char_count": len(l0_text),
                },
            )

        # ── L2 fallback: vision LLM page-by-page ──
        if self._vision is None:
            logger.warning(
                "pdf_loader: scan-only/garbled but vision_client None → placeholder",
            )
            return self._placeholder_result(t0, "scan_only_no_vision_client")

        try:
            text, page_count = await self._vision_l2(file_path)
        except Exception as exc:
            logger.warning("pdf_loader: vision L2 failed: %s", exc)
            # PDFium open/rasterize failure on a valid file → transient (retry).
            return self._placeholder_result(t0, f"vision_l2_failed: {exc}", transient=True)

        if not text:
            return self._placeholder_result(t0, "vision_l2_returned_empty")

        return LoadResult(
            documents=_md_to_documents(text, page_count),
            layer_used="L2",
            duration_ms=int((time.time() - t0) * 1000),
            metadata={
                "page_count": page_count,
                "char_count": len(text),
                "vision_llm_extracted": True,
                "l0_failed": l0_failed,
            },
        )

    async def _pypdfium_l0(self, file_path: str) -> tuple[str, int, bool]:
        """pypdfium2 文本提取 (L0). Returns (text, page_count, l0_failed).

        简单 native PDF 直接提文字 (合同/回单/简历 主场景). 输出按页拼 "## Page N\\n...".
        """
        try:
            text, page_count = await asyncio.to_thread(
                self._extract_with_pypdfium,
                file_path,
            )
            return text or "", page_count, False
        except Exception as exc:
            logger.warning("pypdfium2 L0 failed for %s: %s", file_path, exc)
            return "", 0, True

    async def _vision_l2(self, file_path: str) -> tuple[str, int]:
        """Render PDF pages → PNG → vision LLM (bounded concurrency) → markdown.

        - Pages capped at VISION_PAGE_CAP (default 100). 超 cap 静默丢页 (logged).
        - 并发 VISION_CONCURRENCY (default 4): asyncio.Semaphore 限速,
          stream 模式 render 一页 → 调 LLM → 释放, 不 pre-render 全部 (避内存爆).
        """
        if self._vision is None:
            return "", 0

        # Page count first (light IO), to decide which indices to render.
        full_page_count = await asyncio.to_thread(self._count_pages, file_path)
        target_pages = min(full_page_count, VISION_PAGE_CAP)
        if target_pages < full_page_count:
            logger.info(
                "pdf_loader: doc has %d pages, capping at VISION_PAGE_CAP=%d",
                full_page_count,
                VISION_PAGE_CAP,
            )

        sem = asyncio.Semaphore(VISION_CONCURRENCY)

        async def _process_page(idx: int) -> tuple[int, str]:
            async with sem:
                # Stream render: render this page → call LLM → discard PNG bytes.
                try:
                    b64 = await asyncio.to_thread(
                        self._render_one_page,
                        file_path,
                        idx,
                    )
                except Exception as exc:
                    logger.warning("pdf_loader: page %d render failed: %s", idx + 1, exc)
                    return idx, ""
                try:
                    text = await self._vision.extract_text(b64, mime="image/png")
                except Exception as exc:
                    logger.warning("pdf_loader: page %d vision failed: %s", idx + 1, exc)
                    return idx, ""
                return idx, text

        results = await asyncio.gather(
            *[_process_page(i) for i in range(target_pages)],
        )
        results.sort(key=lambda r: r[0])
        page_texts = [f"## Page {idx + 1}\n\n{text}" for idx, text in results if text]
        return "\n\n".join(page_texts), full_page_count

    @staticmethod
    def _count_pages(file_path: str) -> int:
        """Count pages without rendering. Sync (run in to_thread)."""
        import pypdfium2 as pdfium

        with _PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(file_path)
            try:
                return len(pdf)
            finally:
                pdf.close()

    @staticmethod
    def _render_one_page(file_path: str, page_idx: int) -> str:
        """Render single page → PNG base64. Sync (run in to_thread).

        Stream-friendly alternative to pre-rendering all pages: caller renders +
        passes to LLM + discards in a tight loop, keeping memory ~10MB at any
        moment (1 PNG @ 144DPI) regardless of doc length.
        """
        import pypdfium2 as pdfium

        with _PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(file_path)
            try:
                page = pdf[page_idx]
                try:
                    pil_img = page.render(scale=PDF_RENDER_SCALE).to_pil()
                finally:
                    page.close()
            finally:
                pdf.close()
        # PNG encoding does not touch PDFium; run outside lock (a substantial part of render time). Safety basis (pypdfium2
        # 4.30 source): default render uses native buffer (Python ctypes array, Python-owned,
        # close does not free it) + BGR rawmode absent from PIL _MAPMODES → to_pil uses frombytes
        # **full copy**—pil_img has no link to PDFium memory. Changing render parameters (e.g. grayscale "L"
        # switches to zero-copy frombuffer) requires reassessment and moving back inside lock.
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    @staticmethod
    def _extract_with_pypdfium(file_path: str) -> tuple[str, int]:
        """Sync extract via pypdfium2. 拼 '## Page N\\n<text>' 拿 markdown-ish 输出.

        若文字 < MIN_TEXT_CHARS_FOR_NATIVE, caller 视为 scan-only / garbled, 走 L2 vision.
        """
        import pypdfium2 as pdfium

        # TODO(granularity): whole-document lock—large text PDFs undergoing concurrent _PARSE_SEMAPHORE(5)
        # L0 extraction hold it serially for seconds; only one call at a time is required, so per-page locking is possible. Correctness
        # first, start coarse-grained.
        with _PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(file_path)
            try:
                page_count = len(pdf)
                page_texts: list[str] = []
                for i in range(page_count):
                    page = pdf[i]
                    try:
                        tp = page.get_textpage()
                        text = tp.get_text_range() or ""
                        tp.close()
                    except Exception as exc:
                        logger.warning("pypdfium2 page %d text extract failed: %s", i, exc)
                        text = ""
                    finally:
                        page.close()
                    text = text.strip()
                    if text:
                        page_texts.append(f"## Page {i + 1}\n\n{text}")
                return "\n\n".join(page_texts), page_count
            finally:
                # Previously missing close → document handles relied on GC finalizer, which can run on
                # any thread = another non-thread-safe entry; close explicitly.
                pdf.close()

    @staticmethod
    def _placeholder_result(t0: float, reason: str, *, transient: bool = False) -> LoadResult:
        # transient=True flags a PDFium open/rasterize failure on a valid file
        # (succeeds on a fresh attempt) so the worker retries — vs scan-only /
        # empty, which are permanent and shouldn't be retried.
        return LoadResult(
            documents=[
                KbDocument(
                    page_content=("(PDF parse failed — 扫描件或损坏文件, 在团队设置配置视觉模型以启用 vision LLM L2, 或上传 markdown / docx 替代)"),
                    metadata={"page": None, "parse_failed": True},
                )
            ],
            layer_used="L0",
            duration_ms=int((time.time() - t0) * 1000),
            metadata={"parse_failed": True, "reason": reason, "transient": transient},
        )
