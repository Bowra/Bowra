"""Editable design dogfood: create → edit text → flatten → download."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_tools import _create_design, _update_design_text
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from PIL import Image

from bowhead_core import CanvasStore, SessionStore


@dataclass
class _ToolCtx:
    turn_id: str = "t_design_dogfood"
    user_id: str = "usr_dev"
    workspace_id: str = "w"
    session_id: str = "s_design_dogfood"
    artifact_store: Any = None
    canvas_store: Any = None
    parent_bus: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "1")
    monkeypatch.setenv("BOWHEAD_DESIGN_FONT_ALLOW_FALLBACK", "1")
    db_path = tmp_path / "app.db"
    sessions = SessionStore(db_path)
    await sessions.initialize()
    canvas = CanvasStore(db_path)
    await canvas.initialize()
    artifacts = ArtifactStore(
        backend=LocalBackend(root_dir=tmp_path / "files"),
        db_path=db_path,
    )
    await artifacts.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = sessions
    app.state.canvas_store = canvas
    app.state.artifact_store = artifacts
    app.state.orchestrator = None
    app.state.datarc = None
    return app


def _png_bytes() -> bytes:
    image = Image.new("RGB", (640, 480), (236, 248, 255))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.mark.integration
async def test_editable_design_create_edit_flatten_download_dogfood(app_ctx) -> None:
    """Covers the V2A happy path without invoking an external image model."""
    canvas_store: CanvasStore = app_ctx.state.canvas_store
    artifact_store: ArtifactStore = app_ctx.state.artifact_store
    uid = app_ctx.state.settings.dev_default_user
    session_id = "s_design_dogfood"
    background = await artifact_store.put_bytes(
        content=_png_bytes(),
        filename="milk-tea-bg.png",
        mime="image/png",
        source="image_generate",
        created_by=uid,
        session_id=session_id,
        kind="tool_data",
        inject="stub",
        producer_kind="image_generate",
        created_by_turn="t_design_dogfood",
    )
    ctx = _ToolCtx(
        user_id=uid,
        session_id=session_id,
        artifact_store=artifact_store,
        canvas_store=canvas_store,
    )

    created = await _create_design(
        {
            "background_artifact_id": background.id,
            "title": "奶茶促销海报",
            "size_preset": "portrait_3_4",
            "layers": [
                {
                    "id": "headline",
                    "type": "text",
                    "text": "夏日冰爽",
                    "x": 0.12,
                    "y": 0.12,
                    "w": 0.76,
                    "h": 0.12,
                    "font_size": 76,
                    "font_weight": 800,
                    "color": "#12324A",
                    "align": "center",
                },
                {
                    "id": "cta",
                    "type": "text",
                    "text": "第二杯半价",
                    "x": 0.18,
                    "y": 0.74,
                    "w": 0.64,
                    "h": 0.08,
                    "font_size": 48,
                    "color": "#FFFFFF",
                    "align": "center",
                    "letter_spacing": 1.0,
                },
            ],
        },
        ctx,
    )
    assert created["kind"] == "design"

    patched = await _update_design_text(
        {
            "section_id": created["section_id"],
            "layer_id": "cta",
            "text": "第二杯 5 折",
            "expected_rev": created["rev"],
        },
        ctx,
    )
    assert patched["text"] == "第二杯 5 折"
    assert patched["rev"] == created["rev"] + 1

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        hydrated = await client.get(f"/api/v1/canvas/by-session/{session_id}")
        assert hydrated.status_code == 200, hydrated.text
        section = hydrated.json()["sections"][0]
        assert section["kind"] == "design"
        assert [layer["text"] for layer in section["layers"]] == ["夏日冰爽", "第二杯 5 折"]

        flattened = await client.post(
            f"/api/v1/canvas/{created['canvas_id']}/sections/{created['section_id']}/flatten",
            json={"expected_rev": patched["rev"]},
        )
        assert flattened.status_code == 200, flattened.text
        flattened_body = flattened.json()

        downloaded = await client.get(flattened_body["url"])
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("image/png")
        assert "attachment" in downloaded.headers.get("content-disposition", "")
        assert downloaded.content.startswith(b"\x89PNG\r\n\x1a\n")

        files = await client.get("/api/v1/files/", params={"source": "all"})
        assert files.status_code == 200, files.text

    flattened_artifact_id = flattened_body["artifact_id"]
    assert flattened_body["section"]["flattened_artifact_id"] == flattened_artifact_id
    assert flattened_body["section"]["flattened_source_rev"] == patched["rev"]
    assert flattened_body["section"]["rev"] == patched["rev"] + 1

    flattened_artifact = await artifact_store.get(flattened_artifact_id)
    assert flattened_artifact is not None
    assert flattened_artifact.source == "design_flatten"
    assert flattened_artifact.kind == "deliverable"
    assert flattened_artifact.derived_from == background.id
    provenance = await artifact_store.get_design_flatten_provenance(flattened_artifact_id)
    assert provenance is not None
    assert provenance.canvas_id == created["canvas_id"]
    assert provenance.section_id == created["section_id"]
    assert provenance.section_rev == patched["rev"]
    assert provenance.background_artifact_id == background.id

    file_items = files.json()["items"]
    flattened_file = next(item for item in file_items if item["file_id"] == flattened_artifact_id)
    assert flattened_file["origin"] == "export"
    assert flattened_file["source"] == "design_flatten"
