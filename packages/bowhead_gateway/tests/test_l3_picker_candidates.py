"""Sprint chat-redesign-2026-05-21 Phase 1.1 — L3 picker candidate resolution."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from bowhead_gateway.l3_picker.candidates import (
    CANDIDATES_BY_MIME,
    _mime_to_short,
    candidates_from_attachments,
)


def test_mime_to_short_csv_variants() -> None:
    assert _mime_to_short("text/csv") == "csv"
    assert _mime_to_short("", "report.csv") == "csv"


def test_mime_to_short_xlsx_variants() -> None:
    assert (
        _mime_to_short(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        == "xlsx"
    )
    assert _mime_to_short("", "data.xlsx") == "xlsx"


def test_mime_to_short_pdf() -> None:
    assert _mime_to_short("application/pdf") == "pdf"
    assert _mime_to_short("", "spec.pdf") == "pdf"


def test_mime_to_short_image() -> None:
    assert _mime_to_short("image/png") == "image"
    assert _mime_to_short("image/jpeg") == "image"


def test_mime_to_short_unknown() -> None:
    assert _mime_to_short("application/x-mystery") == "_unknown"
    assert _mime_to_short("") == "_unknown"


@dataclass
class _FakeArtifact:
    id: str
    filename: str
    mime: str
    size: int = 0


class _FakeStore:
    def __init__(self, items: dict[str, _FakeArtifact]) -> None:
        self._items = items

    async def get(self, aid: str) -> _FakeArtifact | None:
        return self._items.get(aid)


@pytest.mark.asyncio
async def test_candidates_empty_returns_unknown_bucket() -> None:
    store = _FakeStore({})
    out = await candidates_from_attachments([], artifact_store=store)
    assert out == CANDIDATES_BY_MIME["_unknown"]


@pytest.mark.asyncio
async def test_candidates_multi_attachment_returns_multi_bucket() -> None:
    store = _FakeStore(
        {
            "a1": _FakeArtifact(id="a1", filename="x.csv", mime="text/csv"),
            "a2": _FakeArtifact(id="a2", filename="y.pdf", mime="application/pdf"),
        }
    )
    out = await candidates_from_attachments(["a1", "a2"], artifact_store=store)
    assert out == CANDIDATES_BY_MIME["_multi"]


@pytest.mark.asyncio
async def test_candidates_single_xlsx_returns_xlsx_bucket() -> None:
    store = _FakeStore(
        {
            "a1": _FakeArtifact(
                id="a1",
                filename="data.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        }
    )
    out = await candidates_from_attachments(["a1"], artifact_store=store)
    assert out == CANDIDATES_BY_MIME["xlsx"]
    assert "做个分析" in out


@pytest.mark.asyncio
async def test_candidates_single_pdf_returns_pdf_bucket() -> None:
    store = _FakeStore(
        {
            "a1": _FakeArtifact(id="a1", filename="spec.pdf", mime="application/pdf"),
        }
    )
    out = await candidates_from_attachments(["a1"], artifact_store=store)
    assert out == CANDIDATES_BY_MIME["pdf"]
    assert "总结一下" in out


@pytest.mark.asyncio
async def test_candidates_unknown_artifact_returns_unknown_bucket() -> None:
    store = _FakeStore({})  # artifact_id not in store
    out = await candidates_from_attachments(["missing"], artifact_store=store)
    assert out == CANDIDATES_BY_MIME["_unknown"]


@pytest.mark.asyncio
async def test_candidates_store_raises_returns_unknown_bucket() -> None:
    class _BoomStore:
        async def get(self, _aid: str) -> _FakeArtifact:
            raise RuntimeError("boom")

    out = await candidates_from_attachments(["a1"], artifact_store=_BoomStore())
    assert out == CANDIDATES_BY_MIME["_unknown"]
