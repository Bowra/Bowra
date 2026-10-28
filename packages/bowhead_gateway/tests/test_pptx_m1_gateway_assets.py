"""Sprint pptx-from-template M1.5 — gateway asset plumbing.

Covers the kind-aware origin resolution (deck/web priority) and the new
asset-model DTO fields surfaced to the Assets panel.
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_gateway.artifact_origin import SOURCE_TO_ORIGIN, origin_for
from bowhead_gateway.dtos import FileListItemDTO
from bowhead_gateway.routes.canvas import _report_artifact_item
from bowhead_gateway.routes.files import _origin_for_art
from bowhead_gateway.routes.sessions import (
    SessionArtifactItemDTO,
    _origin_for_kind_source,
)


def test_web_search_origin_added() -> None:
    assert SOURCE_TO_ORIGIN["web_search"] == "web"
    assert origin_for(None, "bi_fetch_rows") == "query"
    assert origin_for(None, "canvas_export_clone") == "export"


def test_origin_for_art_kind_priority() -> None:
    def mk(kind: str, source: str):
        return SimpleNamespace(kind=kind, source=source)

    # Precise source takes priority; deliverable is not synonymous with deck.
    assert _origin_for_art(mk("deliverable", "sandbox_output")) == "compute"
    assert _origin_for_art(mk("web_clip", "web_search")) == "web"
    # otherwise fall back to source
    assert _origin_for_art(mk("tool_data", "bi_fetch_rows")) == "query"
    assert _origin_for_art(mk("user_upload", "upload")) == "upload"


def test_sessions_origin_helper() -> None:
    assert _origin_for_kind_source("deliverable", "python_sandbox") == "compute"
    assert _origin_for_kind_source("web_clip", "web_search") == "web"
    assert _origin_for_kind_source(None, "bi_fetch_rows") == "query"
    assert _origin_for_kind_source(None, "canvas_export_clone") == "export"
    assert _origin_for_kind_source(None, "kb_upload") == "upload"


def test_report_artifact_item_uses_precise_producer_origin() -> None:
    item = _report_artifact_item(
        SimpleNamespace(
            id="art_export",
            filename="report.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size=1,
            source="legacy_unknown",
            kind="deliverable",
            producer_kind="canvas_export_skill",
            created_at="2026-08-08T00:00:00+00:00",
            created_by_turn="t_1",
            trust="trusted",
        )
    )

    assert item.origin == "export"


def test_dtos_expose_asset_fields() -> None:
    fl = set(FileListItemDTO.model_fields)
    assert {"kind", "trust", "origin"} <= fl
    sa = set(SessionArtifactItemDTO.model_fields)
    assert {
        "kind",
        "origin",
        "trust",
        "created_by_turn",
        "producer_kind",
        "derived_from",
        "manifest_artifact_id",
    } <= sa
    # origin Literal accepts the new web/deck values
    dto = FileListItemDTO(
        file_id="art_x",
        filename="d.pptx",
        mime="x",
        size=1,
        source="sandbox_output",
        origin="deck",
        kind="deliverable",
        trust="trusted",
        created_at="2026",
        url="/x",
    )
    assert dto.origin == "deck" and dto.kind == "deliverable"
