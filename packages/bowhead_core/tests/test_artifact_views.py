"""Kept renders of a stored file (docx inline edit design §5.2)."""

from __future__ import annotations

from pathlib import Path

from bowhead_core.artifact_store import LocalBackend
from bowhead_core.artifact_views import delete_views, read_view, read_view_for_source, view_paths, write_view

PDF = b"%PDF-1.7\nkept render"


async def test_a_render_is_kept_for_its_source_and_renderer(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    await write_view(backend, "art_1", "pptx-pdf", source="etag-1", renderer="r1", pdf=PDF)
    assert await read_view(backend, "art_1", "pptx-pdf", source="etag-1", renderer="r1", limit=1024) == PDF
    assert await read_view(backend, "art_1", "pptx-pdf", source="etag-2", renderer="r1", limit=1024) is None, "Replaced bytes render again"
    assert await read_view(backend, "art_1", "pptx-pdf", source="etag-1", renderer="r2", limit=1024) is None, "A new renderer renders again"
    assert await read_view(backend, "art_1", "docx-pdf", source="etag-1", renderer="r1", limit=1024) is None
    assert await read_view(backend, "art_2", "pptx-pdf", source="etag-1", renderer="r1", limit=1024) is None


async def test_a_reference_reads_the_render_of_its_stored_object_by_any_renderer(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    await write_view(backend, "art_1", "docx-pdf", source="etag-1", renderer="r1", pdf=PDF)
    assert await read_view_for_source(backend, "art_1", "docx-pdf", source="etag-1", limit=1024) == PDF
    assert await read_view_for_source(backend, "art_1", "docx-pdf", source="etag-2", limit=1024) is None, "Another object's pages"
    assert await read_view_for_source(backend, "art_1", "docx-pdf", source="etag-1", limit=len(PDF) - 1) is None
    Path(backend.path_for_artifact("art_1", ".view.docx-pdf.pdf")).write_bytes(b"%PDF-1.7\ntampered!!")
    assert await read_view_for_source(backend, "art_1", "docx-pdf", source="etag-1", limit=1024) is None


async def test_a_damaged_or_oversized_render_is_a_miss(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    await write_view(backend, "art_1", "pptx-pdf", source="s", renderer="r", pdf=PDF)
    assert await read_view(backend, "art_1", "pptx-pdf", source="s", renderer="r", limit=len(PDF) - 1) is None
    Path(backend.path_for_artifact("art_1", ".view.pptx-pdf.pdf")).write_bytes(b"%PDF-1.7\ntampered!!")
    assert await read_view(backend, "art_1", "pptx-pdf", source="s", renderer="r", limit=1024) is None
    Path(backend.path_for_artifact("art_1", ".view.pptx-pdf.json")).write_text("not json")
    assert await read_view(backend, "art_1", "pptx-pdf", source="s", renderer="r", limit=1024) is None


async def test_views_are_deleted_with_their_file_and_never_named_without_a_path_scheme(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    await write_view(backend, "art_1", "pptx-pdf", source="s", renderer="r", pdf=PDF)
    await write_view(backend, "art_1", "docx-pdf", source="s", renderer="r", pdf=PDF)
    assert sum(Path(path).exists() for path in view_paths(backend, "art_1")) == 4
    await delete_views(backend, "art_1")
    await delete_views(backend, "art_1")  # Absent paths are fine.
    assert not any(Path(path).exists() for path in view_paths(backend, "art_1"))

    class NoPaths:
        def __init__(self) -> None:
            self.puts = 0

        async def put(self, **_: object) -> str:
            self.puts += 1
            return ""

    bare = NoPaths()
    await write_view(bare, "art_1", "pptx-pdf", source="s", renderer="r", pdf=PDF)
    assert bare.puts == 0 and view_paths(bare, "art_1") == [], "A view that could not be erased is never written"
