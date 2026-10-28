"""Visual materialization accounting, staging, and renderer fencing."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from bowhead_core.export_assets import (
    ResolvedSnapshotAsset,
    SnapshotAssetResolution,
)
from bowhead_core.export_ir import ExportIRBlock, normalize_export_snapshot
from bowhead_core.export_materializer import (
    BrowserRenderedVisual,
    VisualMaterializationError,
    materialize_export_visuals,
)
from bowhead_core.export_snapshot import ExportSnapshot
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION
from bowhead_core.print_theme import current_print_theme_snapshot
from PIL import Image

from bowhead_core import export_materializer as materializer_module

_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/canvas_export/surface_projection_v1.json"


@dataclass
class FakeBrowserRenderer:
    name: str = "canvas-browser-visual-v1"
    version: str = EXPORT_VISUAL_RENDERER_VERSION
    forged_receipt: bool = False
    wrong_size: bool = False
    calls: list[str] | None = None

    def __post_init__(self) -> None:
        self.calls = []

    async def render(
        self,
        block: ExportIRBlock,
        *,
        asset_paths: dict[str, Path],
    ) -> BrowserRenderedVisual:
        del asset_paths
        assert self.calls is not None
        self.calls.append(block.block_key)
        size = (800, 450) if self.wrong_size else (1600, 900)
        return BrowserRenderedVisual(
            content=_png_bytes(size, (20, 80, 160, 255)),
            mime="image/png",
            visual_input_sha256=(f"sha256:{'f' * 64}" if self.forged_receipt else str(block.visual_input_sha256)),
        )


def _fixture_snapshot() -> ExportSnapshot:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    by_id = {section["id"]: section for section in fixture["sections"]}
    entries = copy.deepcopy(fixture["expected_entries"])
    sections = [copy.deepcopy(by_id[entry["section_id"]]) for entry in entries]
    payload = {
        "request": {
            "format": "pdf",
            "profile": "full_report",
            "parity_contract": "full_content_v1",
            "client_surface_receipt_sha256": f"sha256:{'d' * 64}",
            "locale": "zh-CN",
            "timezone": "Asia/Singapore",
            "theme": {"kind": "default"},
            "print_theme": current_print_theme_snapshot(),
            "options": {"include_appendix": True},
        },
        "surface_projection": {
            "contract_version": 1,
            "token": fixture["surface_token"],
            "entries": entries,
            "exclusions": fixture["expected_exclusions"],
        },
        "sections": sections,
    }
    return _snapshot(
        payload=payload,
        section_count=len(sections),
        surface_token=fixture["surface_token"],
    )


def _snapshot(
    *,
    payload: dict,
    section_count: int,
    surface_token: str,
) -> ExportSnapshot:
    return ExportSnapshot(
        id="exs_materializer",
        schema_version=1,
        canvas_id="cv_materializer",
        session_id="s_materializer",
        team_id="team_materializer",
        workspace_id="ws_materializer",
        canvas_owner="u_owner",
        requested_by="u_owner",
        fmt="pdf",
        profile="full_report",
        title="Materializer fixture",
        locale="zh-CN",
        timezone="Asia/Singapore",
        projection_token=f"sha256:{'a' * 64}",
        surface_contract_version=1,
        surface_token=surface_token,
        parity_contract="full_content_v1",
        payload=payload,
        payload_sha256=f"sha256:{'c' * 64}",
        size_bytes=4096,
        section_count=section_count,
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-24T00:00:00+00:00",
    )


def _stage_asset(
    *,
    attempt_dir: Path,
    artifact_id: str,
    content: bytes,
) -> ResolvedSnapshotAsset:
    assets_dir = attempt_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    name = f"{artifact_id}.png"
    path = assets_dir / name
    path.write_bytes(content)
    return ResolvedSnapshotAsset(
        artifact_id=artifact_id,
        content_sha256=_sha256(content),
        size_bytes=len(content),
        mime="image/png",
        relative_path=f"assets/{name}",
    )


async def test_ir_materializes_each_visual_exactly_once(
    tmp_path: Path,
) -> None:
    attempt_dir = tmp_path / "attempt"
    image = _png_bytes((640, 480), (200, 40, 20, 255))
    background = _png_bytes((1080, 1080), (240, 240, 240, 255))
    resolution = SnapshotAssetResolution(
        resolved=(
            _stage_asset(
                attempt_dir=attempt_dir,
                artifact_id="art_image",
                content=image,
            ),
            _stage_asset(
                attempt_dir=attempt_dir,
                artifact_id="art_bg",
                content=background,
            ),
        ),
        warnings=(),
    )
    export_ir = normalize_export_snapshot(
        _fixture_snapshot(),
        asset_content_hashes={asset.artifact_id: asset.content_sha256 for asset in resolution.resolved},
    )
    renderer = FakeBrowserRenderer()

    visuals = await materialize_export_visuals(
        export_ir=export_ir,
        asset_resolution=resolution,
        attempt_dir=attempt_dir,
        browser_renderer=renderer,
    )

    assert len(visuals) == 5
    assert {item.section_kind for item in visuals} == {
        "chart",
        "image",
        "design",
        "spec_block",
    }
    assert renderer.calls is not None
    assert sorted(renderer.calls) == ["sec_chart", "sec_orphan_chart", "sec_spec"]
    image_output = next(item for item in visuals if item.section_kind == "image")
    design_output = next(item for item in visuals if item.section_kind == "design")
    assert (image_output.width, image_output.height) == (640, 480)
    assert (design_output.width, design_output.height) == (1080, 1080)
    assert image_output.renderer_name == "pinned-image-v1"
    assert design_output.renderer_name == "design-flatten-v2"
    assert all(
        (attempt_dir / item.relative_path).is_file() and ".." not in item.relative_path and item.content_sha256.startswith("sha256:")
        for item in visuals
    )


async def test_fresh_design_flatten_is_reused_without_browser_renderer(
    tmp_path: Path,
) -> None:
    surface_token = f"sha256:{'b' * 64}"
    payload = {
        "request": {
            "theme": {"kind": "default"},
            "print_theme": current_print_theme_snapshot(),
            "client_surface_receipt_sha256": f"sha256:{'d' * 64}",
        },
        "surface_projection": {
            "token": surface_token,
            "entries": [
                {
                    "section_id": "sec_design",
                    "section_rev": 2,
                    "order": 0,
                    "placement": "report_body",
                    "effective_kind": "design",
                }
            ],
            "exclusions": [],
        },
        "sections": [
            {
                "id": "sec_design",
                "kind": "design",
                "rev": 2,
                "payload": {
                    "title": "Fresh",
                    "size_preset": "square",
                    "canvas_width": 320,
                    "canvas_height": 320,
                    "background": {
                        "type": "artifact",
                        "artifact_id": "art_bg",
                    },
                    "layers": [],
                    "flattened_artifact_id": "art_flat",
                    "flattened_source_rev": 1,
                },
            }
        ],
    }
    snapshot = _snapshot(
        payload=payload,
        section_count=1,
        surface_token=surface_token,
    )
    attempt_dir = tmp_path / "fresh"
    background = _png_bytes((320, 320), (240, 240, 240, 255))
    flattened = _png_bytes((320, 320), (30, 40, 50, 255))
    resolution = SnapshotAssetResolution(
        resolved=(
            _stage_asset(
                attempt_dir=attempt_dir,
                artifact_id="art_bg",
                content=background,
            ),
            _stage_asset(
                attempt_dir=attempt_dir,
                artifact_id="art_flat",
                content=flattened,
            ),
        ),
        warnings=(),
    )
    export_ir = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={asset.artifact_id: asset.content_sha256 for asset in resolution.resolved},
    )
    renderer = FakeBrowserRenderer()

    visuals = await materialize_export_visuals(
        export_ir=export_ir,
        asset_resolution=resolution,
        attempt_dir=attempt_dir,
        browser_renderer=renderer,
    )

    assert len(visuals) == 1
    assert visuals[0].renderer_name == "design-flatten-v2"
    assert renderer.calls == []


@pytest.mark.parametrize(
    ("renderer", "expected_code"),
    [
        (
            FakeBrowserRenderer(forged_receipt=True),
            "visual_renderer_receipt_mismatch",
        ),
        (
            FakeBrowserRenderer(wrong_size=True),
            "visual_dimensions_invalid",
        ),
    ],
)
async def test_browser_renderer_receipt_and_dimensions_fail_closed(
    tmp_path: Path,
    renderer: FakeBrowserRenderer,
    expected_code: str,
) -> None:
    attempt_dir = tmp_path / expected_code
    image = _png_bytes((640, 480), (200, 40, 20, 255))
    background = _png_bytes((1080, 1080), (240, 240, 240, 255))
    resolution = SnapshotAssetResolution(
        resolved=(
            _stage_asset(
                attempt_dir=attempt_dir,
                artifact_id="art_image",
                content=image,
            ),
            _stage_asset(
                attempt_dir=attempt_dir,
                artifact_id="art_bg",
                content=background,
            ),
        ),
        warnings=(),
    )
    export_ir = normalize_export_snapshot(
        _fixture_snapshot(),
        asset_content_hashes={asset.artifact_id: asset.content_sha256 for asset in resolution.resolved},
    )

    with pytest.raises(VisualMaterializationError) as error:
        await materialize_export_visuals(
            export_ir=export_ir,
            asset_resolution=resolution,
            attempt_dir=attempt_dir,
            browser_renderer=renderer,
        )
    assert error.value.code == expected_code


async def test_staged_asset_hash_and_path_are_revalidated(
    tmp_path: Path,
) -> None:
    attempt_dir = tmp_path / "tamper"
    image = _png_bytes((640, 480), (200, 40, 20, 255))
    background = _png_bytes((1080, 1080), (240, 240, 240, 255))
    image_asset = _stage_asset(
        attempt_dir=attempt_dir,
        artifact_id="art_image",
        content=image,
    )
    background_asset = _stage_asset(
        attempt_dir=attempt_dir,
        artifact_id="art_bg",
        content=background,
    )
    resolution = SnapshotAssetResolution(
        resolved=(image_asset, background_asset),
        warnings=(),
    )
    export_ir = normalize_export_snapshot(
        _fixture_snapshot(),
        asset_content_hashes={asset.artifact_id: asset.content_sha256 for asset in resolution.resolved},
    )
    tampered = replace(
        background_asset,
        content_sha256=f"sha256:{'0' * 64}",
    )

    with pytest.raises(VisualMaterializationError) as error:
        await materialize_export_visuals(
            export_ir=export_ir,
            asset_resolution=replace(
                resolution,
                resolved=(image_asset, tampered),
            ),
            attempt_dir=attempt_dir,
            browser_renderer=FakeBrowserRenderer(),
        )
    assert error.value.code == "asset_missing_or_forbidden"


def test_png_dimensions_are_rejected_before_rgba_decode() -> None:
    output = io.BytesIO()
    Image.new("1", (4097, 4097), 1).save(output, format="PNG")

    with pytest.raises(
        VisualMaterializationError,
        match="visual_dimensions_invalid",
    ):
        materializer_module._normalize_png(output.getvalue())  # noqa: SLF001


def _png_bytes(
    size: tuple[int, int],
    color: tuple[int, int, int, int],
) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
