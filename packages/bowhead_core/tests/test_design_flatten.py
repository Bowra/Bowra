"""Flatten editable design sections into PNG artifacts."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.design_renderer import (
    DesignFlattenError,
    _render_design_png,
    flatten_design_section,
)
from PIL import Image

from bowhead_core import CanvasRevConflictError, CanvasStore
from bowhead_core import metrics as core_metrics

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_metrics():
    core_metrics.get_registry().reset()
    yield
    core_metrics.get_registry().reset()


def _png_bytes(color: tuple[int, int, int] = (240, 248, 255)) -> bytes:
    img = Image.new("RGB", (80, 60), color)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def _stores(tmp_path: Path) -> tuple[CanvasStore, ArtifactStore]:
    canvas = CanvasStore(tmp_path / "c.db")
    await canvas.initialize()
    art = ArtifactStore(
        backend=LocalBackend(root_dir=tmp_path / "files"),
        db_path=tmp_path / "a.db",
    )
    await art.initialize()
    return canvas, art


async def _put_artifact(
    artifact_store: ArtifactStore,
    *,
    content: bytes | None = None,
    mime: str = "image/png",
    created_by: str = "user_1",
    filename: str = "bg.png",
) -> Any:
    return await artifact_store.put_bytes(
        content=content if content is not None else _png_bytes(),
        filename=filename,
        mime=mime,
        source="image_generate",
        created_by=created_by,
        session_id="sess_1",
        kind="tool_data",
        inject="stub",
        producer_kind="image_generate",
    )


async def _append_design(
    canvas_store: CanvasStore,
    *,
    canvas_id: str,
    background_artifact_id: str | None,
    layers: list[dict[str, Any]] | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "title": "Promo",
        "size_preset": "square_1_1",
        "canvas_width": 320,
        "canvas_height": 240,
        "layers": layers
        if layers is not None
        else [
            {
                "id": "headline",
                "type": "text",
                "text": "SUMMER",
                "x": 0.1,
                "y": 0.1,
                "w": 0.8,
                "h": 0.2,
                "font_size": 32,
                "color": "#111111",
                "align": "center",
                "z": 10,
            },
        ],
        "flattened_artifact_id": None,
        "flattened_source_rev": None,
        "flattened_at": None,
    }
    if background_artifact_id is not None:
        payload["background"] = {
            "type": "artifact",
            "artifact_id": background_artifact_id,
            "fit": "cover",
            "opacity": 1,
        }
    return await canvas_store.append_section(
        canvas_id=canvas_id,
        kind="design",
        payload=payload,
        by_turn_id="turn_1",
    )


async def test_flatten_design_writes_png_and_artifact_side_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_DESIGN_FONT_ALLOW_FALLBACK", "1")
    canvas_store, artifact_store = await _stores(tmp_path)
    canvas = await canvas_store.ensure_canvas(
        session_id="sess_1",
        workspace_id="ws_1",
        user_id="user_1",
    )
    background = await artifact_store.put_bytes(
        content=_png_bytes(),
        filename="bg.png",
        mime="image/png",
        source="image_generate",
        created_by="user_1",
        session_id="sess_1",
        kind="tool_data",
        inject="stub",
        producer_kind="image_generate",
    )
    section = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="design",
        payload={
            "title": "Promo",
            "size_preset": "square_1_1",
            "canvas_width": 320,
            "canvas_height": 240,
            "background": {
                "type": "artifact",
                "artifact_id": background.id,
                "fit": "cover",
                "opacity": 1,
            },
            "layers": [
                {
                    "id": "headline",
                    "type": "text",
                    "text": "SUMMER",
                    "x": 0.1,
                    "y": 0.1,
                    "w": 0.8,
                    "h": 0.2,
                    "font_size": 32,
                    "color": "#111111",
                    "align": "center",
                    "z": 10,
                },
            ],
            "flattened_artifact_id": None,
            "flattened_source_rev": None,
            "flattened_at": None,
        },
        by_turn_id="turn_1",
    )

    result = await flatten_design_section(
        artifact_store=artifact_store,
        canvas_store=canvas_store,
        canvas_id=canvas.id,
        section_id=section.id,
        user_id="user_1",
        session_id="sess_1",
        expected_rev=section.rev,
        created_by_turn="turn_1",
    )

    artifact = await artifact_store.get(result.artifact_id)
    assert artifact is not None
    assert artifact.source == "design_flatten"
    assert artifact.kind == "deliverable"
    assert artifact.inject == "stub"
    assert artifact.producer_kind == "design_flatten"
    assert artifact.derived_from == background.id
    flattened_bytes = await artifact_store.open_read(artifact)
    flattened = Image.open(io.BytesIO(flattened_bytes))
    assert flattened.size == (320, 240)

    provenance = await artifact_store.get_design_flatten_provenance(result.artifact_id)
    assert provenance is not None
    assert provenance.canvas_id == canvas.id
    assert provenance.section_id == section.id
    assert provenance.section_rev == section.rev
    assert provenance.size_preset == "square_1_1"
    assert provenance.background_artifact_id == background.id

    sections = await canvas_store.list_sections(canvas.id)
    updated = next(s for s in sections if s.id == section.id)
    assert updated.payload["flattened_artifact_id"] == result.artifact_id
    assert updated.payload["flattened_source_rev"] == section.rev
    assert result.section["flattened_artifact_id"] == result.artifact_id
    assert result.rev == section.rev + 1
    rendered_metrics = core_metrics.get_registry().render_prometheus()
    assert "bowhead_design_flatten_total" in rendered_metrics
    assert 'status="done"' in rendered_metrics
    assert "bowhead_design_flatten_duration_ms" in rendered_metrics


def test_render_design_draws_first_line_when_text_box_is_tiny(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_DESIGN_FONT_ALLOW_FALLBACK", "1")
    payload = {
        "canvas_width": 160,
        "canvas_height": 80,
        "background": {"type": "artifact", "artifact_id": "art_bg", "fit": "cover"},
        "layers": [
            {
                "id": "headline",
                "type": "text",
                "text": "SUMMER",
                "x": 0,
                "y": 0,
                "w": 1,
                "h": 0.02,
                "font_size": 72,
                "color": "#111111",
                "align": "left",
                "z": 1,
            },
        ],
    }

    content, warnings = _render_design_png(payload, _png_bytes((255, 255, 255)))

    flattened = Image.open(io.BytesIO(content)).convert("RGBA")
    assert any(pixel[:3] != (255, 255, 255) for pixel in flattened.getdata())
    assert "text_overflow:headline" in warnings


async def test_flatten_design_rejects_stale_rev_before_rendering(
    tmp_path: Path,
) -> None:
    canvas_store, artifact_store = await _stores(tmp_path)
    canvas = await canvas_store.ensure_canvas(
        session_id="sess_1",
        workspace_id="ws_1",
        user_id="user_1",
    )
    background = await _put_artifact(artifact_store)
    section = await _append_design(
        canvas_store,
        canvas_id=canvas.id,
        background_artifact_id=background.id,
    )

    with pytest.raises(DesignFlattenError) as excinfo:
        await flatten_design_section(
            artifact_store=artifact_store,
            canvas_store=canvas_store,
            canvas_id=canvas.id,
            section_id=section.id,
            user_id="user_1",
            session_id="sess_1",
            expected_rev=section.rev + 1,
        )

    assert excinfo.value.code == "rev_conflict"
    assert excinfo.value.status_code == 409
    assert excinfo.value.current_rev == section.rev


async def test_flatten_design_soft_deletes_artifact_on_post_render_rev_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    canvas_store, artifact_store = await _stores(tmp_path)
    canvas = await canvas_store.ensure_canvas(
        session_id="sess_1",
        workspace_id="ws_1",
        user_id="user_1",
    )
    background = await _put_artifact(artifact_store)
    section = await _append_design(
        canvas_store,
        canvas_id=canvas.id,
        background_artifact_id=background.id,
    )
    monkeypatch.setattr(
        "bowhead_core.design_renderer._render_design_png",
        lambda _payload, _background_bytes: (_png_bytes(), []),
    )

    async def conflict_patch(*_args: Any, **_kwargs: Any) -> Any:
        raise CanvasRevConflictError(section.rev + 1)

    monkeypatch.setattr(canvas_store, "patch_section", conflict_patch)

    with pytest.raises(DesignFlattenError) as excinfo:
        await flatten_design_section(
            artifact_store=artifact_store,
            canvas_store=canvas_store,
            canvas_id=canvas.id,
            section_id=section.id,
            user_id="user_1",
            session_id="sess_1",
            expected_rev=section.rev,
        )

    assert excinfo.value.code == "rev_conflict"
    live, total, _ = await artifact_store.list_for_user(
        user_id="user_1",
        sources=["design_flatten"],
    )
    assert live == []
    assert total == 0
    deleted, deleted_total, _ = await artifact_store.list_for_user(
        user_id="user_1",
        sources=["design_flatten"],
        include_deleted=True,
    )
    assert deleted_total == 1
    assert deleted[0].deleted_at is not None


async def test_flatten_design_requires_background(tmp_path: Path) -> None:
    canvas_store, artifact_store = await _stores(tmp_path)
    canvas = await canvas_store.ensure_canvas(
        session_id="sess_1",
        workspace_id="ws_1",
        user_id="user_1",
    )
    section = await _append_design(
        canvas_store,
        canvas_id=canvas.id,
        background_artifact_id=None,
    )

    with pytest.raises(DesignFlattenError) as excinfo:
        await flatten_design_section(
            artifact_store=artifact_store,
            canvas_store=canvas_store,
            canvas_id=canvas.id,
            section_id=section.id,
            user_id="user_1",
            session_id="sess_1",
            expected_rev=section.rev,
        )

    assert excinfo.value.code == "background_missing"
    assert excinfo.value.status_code == 400


async def test_flatten_design_rejects_cross_user_background(tmp_path: Path) -> None:
    canvas_store, artifact_store = await _stores(tmp_path)
    canvas = await canvas_store.ensure_canvas(
        session_id="sess_1",
        workspace_id="ws_1",
        user_id="user_1",
    )
    background = await _put_artifact(artifact_store, created_by="user_2")
    section = await _append_design(
        canvas_store,
        canvas_id=canvas.id,
        background_artifact_id=background.id,
    )

    with pytest.raises(DesignFlattenError) as excinfo:
        await flatten_design_section(
            artifact_store=artifact_store,
            canvas_store=canvas_store,
            canvas_id=canvas.id,
            section_id=section.id,
            user_id="user_1",
            session_id="sess_1",
            expected_rev=section.rev,
        )

    assert excinfo.value.code == "background_not_found"
    assert excinfo.value.status_code == 404


async def test_flatten_design_rejects_svg_background(tmp_path: Path) -> None:
    canvas_store, artifact_store = await _stores(tmp_path)
    canvas = await canvas_store.ensure_canvas(
        session_id="sess_1",
        workspace_id="ws_1",
        user_id="user_1",
    )
    background = await _put_artifact(
        artifact_store,
        content=b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        filename="bg.svg",
        mime="image/svg+xml",
    )
    section = await _append_design(
        canvas_store,
        canvas_id=canvas.id,
        background_artifact_id=background.id,
    )

    with pytest.raises(DesignFlattenError) as excinfo:
        await flatten_design_section(
            artifact_store=artifact_store,
            canvas_store=canvas_store,
            canvas_id=canvas.id,
            section_id=section.id,
            user_id="user_1",
            session_id="sess_1",
            expected_rev=section.rev,
        )

    assert excinfo.value.code == "background_not_image"
    assert excinfo.value.status_code == 400


async def test_flatten_design_font_missing_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("BOWHEAD_DESIGN_FONT_ALLOW_FALLBACK", raising=False)
    monkeypatch.setattr("bowhead_core.design_renderer._font_candidates", list)
    canvas_store, artifact_store = await _stores(tmp_path)
    canvas = await canvas_store.ensure_canvas(
        session_id="sess_1",
        workspace_id="ws_1",
        user_id="user_1",
    )
    background = await _put_artifact(artifact_store)
    section = await _append_design(
        canvas_store,
        canvas_id=canvas.id,
        background_artifact_id=background.id,
    )

    with pytest.raises(DesignFlattenError) as excinfo:
        await flatten_design_section(
            artifact_store=artifact_store,
            canvas_store=canvas_store,
            canvas_id=canvas.id,
            section_id=section.id,
            user_id="user_1",
            session_id="sess_1",
            expected_rev=section.rev,
        )

    assert excinfo.value.code == "font_missing"
    assert excinfo.value.status_code == 503
    rendered_metrics = core_metrics.get_registry().render_prometheus()
    assert 'status="font_missing"' in rendered_metrics
