from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.artifact_store import Artifact
from bowhead_gateway.routes import files


class _CanvasStore:
    def __init__(self, canvas, sections) -> None:
        self.canvas = canvas
        self.sections = sections

    async def get_canvas(self, _canvas_id: str):
        return self.canvas

    async def list_sections(self, _canvas_id: str):
        return self.sections


def _artifact() -> SimpleNamespace:
    return SimpleNamespace(
        id="art_export",
        filename="report.pdf",
        source="canvas_export",
        canvas_id="cv_1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "canvas", "sections", "expected"),
    [
        (True, None, [], "export_copy_current"),
        (False, None, [], "export_copy_legacy_source_missing"),
        (
            False,
            SimpleNamespace(id="cv_1", user_id="u_other", status="active"),
            [SimpleNamespace(quarantined=False)],
            "export_copy_legacy_no_permission",
        ),
        (
            False,
            SimpleNamespace(id="cv_1", user_id="u_owner", status="active"),
            [SimpleNamespace(quarantined=True)],
            "export_copy_legacy_source_quarantined",
        ),
        (
            False,
            SimpleNamespace(id="cv_1", user_id="u_owner", status="archived"),
            [SimpleNamespace(quarantined=False)],
            "export_copy_legacy_source_not_exportable",
        ),
        (
            False,
            SimpleNamespace(id="cv_1", user_id="u_owner", status="active"),
            [SimpleNamespace(quarantined=False)],
            "export_copy_legacy_regenerable",
        ),
    ],
)
async def test_export_history_trust_projection_has_finite_states(
    monkeypatch: pytest.MonkeyPatch,
    current: bool,
    canvas,
    sections,
    expected: str,
) -> None:
    artifact = _artifact()

    async def classify(_artifacts, *, artifact_store):
        return {artifact.id: 3} if current else {}

    monkeypatch.setattr(
        "bowhead_core.export_copy_contract.classify_export_artifact_versions",
        classify,
    )
    monkeypatch.setattr(files, "_user_id", lambda _request: "u_owner")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(canvas_store=_CanvasStore(canvas, sections))),
    )
    projected = await files._export_trust_projection(
        request,
        SimpleNamespace(),
        [artifact],
    )
    assert projected[artifact.id] == expected


@pytest.mark.asyncio
async def test_category_response_reuses_export_trust_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = Artifact(
        id="art_export",
        filename="report.pdf",
        mime="application/pdf",
        size=9,
        source="canvas_export",
        path="/tmp/report.pdf",
        canvas_id="cv_1",
        created_by="u_owner",
        created_at="2026-08-01T00:00:00Z",
        trust="trusted",
        category="dataset",
        category_source="user",
    )

    class Store:
        async def get(self, _artifact_id: str, *, include_deleted: bool = False):
            return artifact

        async def set_category(self, _artifact_id: str, *, category: str, source: str) -> None:
            artifact.category = category
            artifact.category_source = source

        async def usage_count(self, _artifact_id: str) -> int:
            return 0

    async def require_operation(*_args, **_kwargs) -> None:
        return None

    async def projection(_request, _store, artifacts):
        return {artifacts[0].id: "export_copy_legacy_regenerable"}

    monkeypatch.setattr(files, "_store", lambda _request: Store())
    monkeypatch.setattr(files, "_user_id", lambda _request: "u_owner")
    monkeypatch.setattr(files, "_require_artifact_operation", require_operation)
    monkeypatch.setattr(files, "_export_trust_projection", projection)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = await files.set_category(
        artifact.id,
        files.CategoryRequest(category="dataset"),
        request,
    )

    assert result.trust == "export_copy_legacy_regenerable"
