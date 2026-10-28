"""image-extraction Day 2: ImageLoader unit tests (mock vision_client).

Tech design v1.0 §3.1 — 4 cases + dispatch.
"""

from __future__ import annotations

import pytest
from bowhead_core.kb.loader.dispatch import get_loader
from bowhead_core.kb.loader.image_loader import ImageLoader
from bowhead_core.kb.vision_llm import VisionLLMError


class _FakeVisionClient:
    def __init__(self, response: str = "", *, raise_exc: Exception | None = None) -> None:
        self.response = response
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, str]] = []

    async def extract_text(self, image_b64: str, *, mime: str = "image/png", **kwargs):
        self.calls.append((image_b64[:20], mime))
        if self.raise_exc:
            raise self.raise_exc
        return self.response


async def test_image_loader_happy_path(tmp_path) -> None:
    """jpg → vision_client extract → markdown text."""
    img_path = tmp_path / "test.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")

    fake = _FakeVisionClient(response="# 身份证\n姓名: 张三")
    loader = ImageLoader(vision_client=fake)
    result = await loader.load(str(img_path))

    assert result.layer_used == "L2"
    assert len(result.documents) == 1
    assert "# 身份证" in result.documents[0].page_content
    assert result.metadata.get("vision_llm_extracted") is True
    assert not result.metadata.get("parse_failed")
    assert len(fake.calls) == 1
    assert fake.calls[0][1] == "image/jpeg"  # mime auto-detected from .jpg


async def test_image_loader_png_mime_dispatch(tmp_path) -> None:
    """png ext → image/png mime."""
    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNGfake")
    fake = _FakeVisionClient(response="OCR text")
    result = await ImageLoader(vision_client=fake).load(str(img_path))
    assert fake.calls[0][1] == "image/png"
    assert result.documents[0].page_content == "OCR text"


async def test_image_loader_vision_failure_returns_placeholder(tmp_path) -> None:
    """vision_llm transient err → placeholder + parse_failed=True."""
    img_path = tmp_path / "test.jpg"
    img_path.write_bytes(b"fake")
    fake = _FakeVisionClient(raise_exc=VisionLLMError("transient timeout"))
    result = await ImageLoader(vision_client=fake).load(str(img_path))
    assert result.metadata.get("parse_failed") is True
    assert "失败" in result.documents[0].page_content


async def test_image_loader_no_vision_client(tmp_path) -> None:
    """vision_client None → placeholder (no API call)."""
    img_path = tmp_path / "test.jpg"
    img_path.write_bytes(b"fake")
    result = await ImageLoader(vision_client=None).load(str(img_path))
    assert result.metadata.get("parse_failed") is True
    assert "vision_client_unconfigured" in result.metadata.get("reason", "")


async def test_image_loader_decorative_returns_empty_doc_not_failed(tmp_path) -> None:
    """vision_llm 返 empty (装饰 / logo) → placeholder.decorative=True (NOT parse_failed)."""
    img_path = tmp_path / "logo.png"
    img_path.write_bytes(b"fake")
    fake = _FakeVisionClient(response="")
    result = await ImageLoader(vision_client=fake).load(str(img_path))
    assert result.metadata.get("decorative") is True
    assert not result.metadata.get("parse_failed")


def test_dispatch_image_mime_returns_image_loader() -> None:
    """get_loader('image/jpeg') → ImageLoader instance with vision_client."""
    fake = _FakeVisionClient()
    loader = get_loader("image/jpeg", vision_client=fake)
    assert isinstance(loader, ImageLoader)


def test_dispatch_unknown_image_mime_raises() -> None:
    """get_loader('image/tiff') → UnsupportedMimeError (V1 仅 jpg/png/webp)."""
    from bowhead_core.kb.errors import UnsupportedMimeError

    with pytest.raises(UnsupportedMimeError):
        get_loader("image/tiff")
