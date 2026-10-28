"""image-extraction Day 3: PdfLoader L0→L2 fallback tests.

Tech design v1.0 §3.1 — 4 cases:
1. marker L0 valid text → 不 fallback, return L0
2. marker L0 仅 figure md (scan-only) → vision_client present → fallback L2
3. marker L0 garbled → vision_client present → fallback L2
4. marker L0 scan-only + vision_client None → placeholder + parse_failed=True
"""

from __future__ import annotations

from unittest.mock import patch

from bowhead_core.kb.loader.pdf_loader import (
    PdfLoader,
    _is_garbled,
    _is_scan_only,
)


class _FakeVisionClient:
    def __init__(self, response_per_page: list[str] | None = None) -> None:
        self.response_per_page = response_per_page or []
        self.call_count = 0

    async def extract_text(self, image_b64: str, *, mime: str = "image/png", **kwargs):
        idx = self.call_count
        self.call_count += 1
        if idx < len(self.response_per_page):
            return self.response_per_page[idx]
        return ""


# ─── _is_scan_only / _is_garbled unit tests ─────────────


def test_is_scan_only_pure_figure_md() -> None:
    text = "![](_page_0_Figure_0.jpeg)\n\n![](_page_0_Figure_1.jpeg)"
    assert _is_scan_only(text) is True


def test_is_scan_only_native_text() -> None:
    text = "# Resume\n\nName: 刘沂鑫\nExperience: 8 years in data analytics, 创业 2018-now."
    assert _is_scan_only(text) is False


def test_is_scan_only_empty() -> None:
    assert _is_scan_only("") is True


def test_is_garbled_normal_text() -> None:
    assert _is_garbled("normal Chinese 中文 text") is False


# ─── PdfLoader fallback tests ────────────────────────


async def test_pdf_loader_native_text_no_fallback(tmp_path) -> None:
    """marker L0 → valid native text → return L0 (no vision LLM call)."""
    fake_vision = _FakeVisionClient()
    loader = PdfLoader(vision_client=fake_vision)

    pdf_path = tmp_path / "native.pdf"
    pdf_path.write_bytes(b"fake_pdf_bytes")

    with patch.object(
        PdfLoader,
        "_pypdfium_l0",
        return_value=(
            "# Document Title\n\nThis is a substantial paragraph with native PDF text. It should not trigger scan-only fallback to vision LLM.",
            5,
            False,
        ),
    ):
        result = await loader.load(str(pdf_path))

    assert result.layer_used == "L0"
    assert "native PDF text" in result.documents[0].page_content
    assert fake_vision.call_count == 0


async def test_pdf_loader_scan_only_fallback_to_l2(tmp_path) -> None:
    """marker L0 仅 figure md → vision_client present → fallback L2."""
    fake_vision = _FakeVisionClient(
        response_per_page=["姓名: 沙海洲\n身份证号: 1101**", "(背面图片)"],
    )
    loader = PdfLoader(vision_client=fake_vision)

    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"fake_pdf")

    with (
        patch.object(
            PdfLoader,
            "_pypdfium_l0",
            return_value=("![](_page_0_Figure_0.jpeg)\n\n![](_page_0_Figure_1.jpeg)", 1, False),
        ),
        patch.object(PdfLoader, "_count_pages", return_value=2),
        patch.object(
            PdfLoader,
            "_render_one_page",
            side_effect=lambda _, idx: f"fake_page_{idx + 1}_b64",
        ),
    ):
        result = await loader.load(str(pdf_path))

    assert result.layer_used == "L2"
    assert "沙海洲" in result.documents[0].page_content
    assert "Page 1" in result.documents[0].page_content
    assert fake_vision.call_count == 2


async def test_pdf_loader_scan_only_no_vision_client_returns_placeholder(tmp_path) -> None:
    """marker L0 scan-only + vision_client None → placeholder + parse_failed=True."""
    loader = PdfLoader(vision_client=None)
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"fake_pdf")

    with patch.object(
        PdfLoader,
        "_pypdfium_l0",
        return_value=("![](_page_0_Figure_0.jpeg)", 1, False),
    ):
        result = await loader.load(str(pdf_path))

    assert result.metadata.get("parse_failed") is True
    assert "no_vision_client" in result.metadata.get("reason", "")


async def test_pdf_loader_marker_failed_no_vision_returns_placeholder(tmp_path) -> None:
    """marker L0 raises (e.g. corrupt PDF) + vision_client None → placeholder."""
    loader = PdfLoader(vision_client=None)
    pdf_path = tmp_path / "broken.pdf"
    pdf_path.write_bytes(b"fake_pdf")

    with patch.object(
        PdfLoader,
        "_pypdfium_l0",
        return_value=("", 0, True),  # l0_failed=True
    ):
        result = await loader.load(str(pdf_path))

    assert result.metadata.get("parse_failed") is True


# ─── Long PDFs untruncated + concurrency (#292 fix) ─────────────


async def test_pdf_loader_l2_handles_30_page_doc(tmp_path) -> None:
    """30 页 scan-only PDF: 全 30 页都过 vision LLM (旧 cap=20 会丢 21-30)."""
    responses = [f"page {i + 1} text" for i in range(30)]
    fake_vision = _FakeVisionClient(response_per_page=responses)
    loader = PdfLoader(vision_client=fake_vision)

    pdf_path = tmp_path / "long_scan.pdf"
    pdf_path.write_bytes(b"fake_pdf")

    with (
        patch.object(
            PdfLoader,
            "_pypdfium_l0",
            return_value=("![](_page_0_Figure_0.jpeg)", 30, False),
        ),
        patch.object(PdfLoader, "_count_pages", return_value=30),
        patch.object(
            PdfLoader,
            "_render_one_page",
            side_effect=lambda _, idx: f"b64_p{idx}",
        ),
    ):
        result = await loader.load(str(pdf_path))

    assert result.layer_used == "L2"
    # Vision LLM processed all 30 pages.
    assert fake_vision.call_count == 30
    # Output ordered by page index (reorder concurrent results); verify order.
    content = result.documents[0].page_content
    for i in range(1, 31):
        assert f"## Page {i}\n\npage {i} text" in content
    # Output order: Page 1 before Page 30.
    assert content.find("Page 1\n") < content.find("Page 30\n")


async def test_pdf_loader_l2_concurrency_bounded(tmp_path, monkeypatch) -> None:
    """VISION_CONCURRENCY=2 → 同时 in-flight 调用数 ≤ 2."""
    import bowhead_core.kb.loader.pdf_loader as pdf_loader_mod

    monkeypatch.setattr(pdf_loader_mod, "VISION_CONCURRENCY", 2)

    in_flight = 0
    max_in_flight = 0

    class _ConcurrencyTrackingVision:
        async def extract_text(self, image_b64, *, mime="image/png", **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                # Yield event loop to simulate async real LLM calls.
                import asyncio

                await asyncio.sleep(0.01)
                return f"text {image_b64}"
            finally:
                in_flight -= 1

    loader = PdfLoader(vision_client=_ConcurrencyTrackingVision())
    pdf_path = tmp_path / "x.pdf"
    pdf_path.write_bytes(b"x")

    with (
        patch.object(
            PdfLoader,
            "_pypdfium_l0",
            return_value=("![](_page_0_Figure_0.jpeg)", 10, False),
        ),
        patch.object(PdfLoader, "_count_pages", return_value=10),
        patch.object(
            PdfLoader,
            "_render_one_page",
            side_effect=lambda _, idx: f"p{idx}",
        ),
    ):
        result = await loader.load(str(pdf_path))

    assert result.layer_used == "L2"
    assert max_in_flight <= 2
    assert max_in_flight >= 2  # Concurrency actually used, no serial degradation.
