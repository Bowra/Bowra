"""Sprint ι Phase B (PRD §4.B.3, tech-r2 §1.6 + §2.3).

Verify:
1. ``_SOURCE_TO_ORIGIN`` maps the canonical file origins retained by the current
   artifact producers.
2. Unknown sources fall back to explicit ``other``（file-management-v2 P0-2 改写；
   Sprint ι 原为 ``upload``，那会让系统产物伪装成用户上传）。
3. ``/files/upload`` source allowlist accepts known sources, falls back on unknown.
4. ``FileListItemDTO.origin`` derived correctly via ``_art_to_dto`` for each canonical source.
"""

from __future__ import annotations

import pytest

# file-management-v2 P0 consolidation: sole source→origin implementation moved to artifact_origin.py.
from bowhead_gateway.artifact_origin import SOURCE_TO_ORIGIN, origin_for
from bowhead_gateway.routes.files import (
    _ALLOWED_UPLOAD_SOURCES,
    _art_to_dto,
    _origin_for_art,
)
from bowhead_gateway.routes.sessions import _origin_for_kind_source


class _FakeArt:
    """Minimal duck-typed stand-in for ArtifactStore.Artifact."""

    def __init__(
        self,
        source: str,
        *,
        kind: str = "tool_data",
        producer_kind: str | None = None,
    ) -> None:
        self.id = "art_test_id"
        self.filename = "test.csv"
        self.mime = "text/csv"
        self.size = 1234
        self.source = source
        self.kind = kind
        self.producer_kind = producer_kind
        self.created_at = "2026-05-09T00:00:00Z"
        self.last_used_at = None
        self.deleted_at = None
        self.category = None
        self.category_source = None


@pytest.mark.parametrize(
    "source,expected_origin",
    [
        ("upload", "upload"),
        ("team_branding", "upload"),
        ("canvas_export", "export"),
        ("python_sandbox", "compute"),
        ("sandbox_output", "compute"),
        ("bi_fetch_rows", "query"),
    ],
)
def test_origin_mapping_covers_every_known_source(source: str, expected_origin: str) -> None:
    assert origin_for(None, source) == expected_origin
    assert SOURCE_TO_ORIGIN[source] == expected_origin


def test_origin_fallback_for_unknown_source() -> None:
    # ⚠ This assertion was **deliberately rewritten** in file-management-v2 P0-2: Sprint ι originally asserted
    # `== "upload"` (docstring also says "falls back to upload (defensive)").
    # This changes a product decision, not a bug fix: unknown sources falling back to "upload" make system outputs
    # masquerade as user uploads (AI-generated design background images once displayed as '上传' for this reason).
    # PRD v2 P0-2 decided fallback must explicitly be "other".
    assert origin_for(None, "totally_unknown_future_source") == "other"


def test_art_to_dto_populates_origin() -> None:
    art = _FakeArt(source="python_sandbox")
    dto = _art_to_dto(art, usage_count=2)
    assert dto.origin == "compute"
    assert dto.source == "python_sandbox"
    assert dto.file_id == "art_test_id"
    assert dto.usage_count == 2


def test_design_flatten_deliverable_origin_is_export() -> None:
    art = _FakeArt(
        source="design_flatten",
        kind="deliverable",
        producer_kind="design_flatten",
    )
    assert _origin_for_art(art) == "export"
    assert _origin_for_kind_source("deliverable", "design_flatten") == "export"


def test_upload_allowlist_includes_canonical_sources() -> None:
    expected = {"upload", "python_sandbox", "sandbox_output", "canvas_export"}
    assert expected.issubset(_ALLOWED_UPLOAD_SOURCES)


def test_upload_allowlist_excludes_query_only_sources() -> None:
    # bi_fetch_rows / kb_extracted_record never come through HTTP /files/upload —
    # ArtifactStore.put_bytes(source=...) writes them directly.
    assert "bi_fetch_rows" not in _ALLOWED_UPLOAD_SOURCES
    assert "kb_extracted_record" not in _ALLOWED_UPLOAD_SOURCES


# Sprint ι Phase B P2 carry-over (from ι-12 review):
# upload source resolution must skip invalid candidates so a malicious or
# typo'd header can't override a legitimate query-string value.
def test_resolve_upload_source_helper_picks_first_valid() -> None:
    """Reusable helper would live in routes/files; we duplicate the logic
    here as a contract-level guard so future refactors don't reintroduce
    'header always wins, even if invalid' behavior.
    """
    from bowhead_gateway.routes.files import _ALLOWED_UPLOAD_SOURCES

    def _resolve(header: str | None, query: str | None) -> str:
        h = (header or "").lower()
        q = (query or "").lower()
        candidates = [c for c in (h, q) if c]
        return next(
            (c for c in candidates if c in _ALLOWED_UPLOAD_SOURCES),
            "upload",
        )

    # Both valid → header wins (more specific signal from runner).
    assert _resolve("python_sandbox", "upload") == "python_sandbox"
    # Header invalid + query valid → use query (P2 carry fix).
    assert _resolve("../etc/passwd", "python_sandbox") == "python_sandbox"
    # Both invalid → fall back to "upload".
    assert _resolve("malicious", "also_bogus") == "upload"
    # Header empty, query valid → use query.
    assert _resolve("", "canvas_export") == "canvas_export"
    # Header valid, no query → use header.
    assert _resolve("python_sandbox", None) == "python_sandbox"
    # Both empty / missing → default upload.
    assert _resolve(None, None) == "upload"
