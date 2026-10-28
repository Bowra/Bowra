"""Snapshot-scoped visual materialization for Canvas ExportIR v1."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from bowhead_core.design_renderer import DesignFlattenError, render_design_png
from bowhead_core.error_catalog import CodedError
from bowhead_core.export_assets import ResolvedSnapshotAsset, SnapshotAssetResolution
from bowhead_core.export_ir import ExportIR, ExportIRBlock, iter_export_blocks
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION

MAX_VISUAL_BYTES = 50 * 1024 * 1024
MAX_VISUAL_DIMENSION = 4096
MAX_VISUAL_PIXELS = 4096 * 4096
CHART_VISUAL_SIZE = (1600, 900)


class VisualMaterializationError(CodedError):
    """A required visual cannot be materialized without semantic drift."""


@dataclass(frozen=True, slots=True)
class BrowserRenderedVisual:
    content: bytes
    mime: str
    visual_input_sha256: str
    warnings: tuple[str, ...] = ()


class BrowserVisualRenderer(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def render(
        self,
        block: ExportIRBlock,
        *,
        asset_paths: dict[str, Path],
    ) -> BrowserRenderedVisual: ...


@dataclass(frozen=True, slots=True)
class MaterializedVisual:
    block_key: str
    section_id: str
    section_rev: int
    section_kind: str
    visual_input_sha256: str
    source_artifact_hashes: tuple[tuple[str, str], ...]
    relative_path: str
    mime: str
    content_sha256: str
    pixel_sha256: str
    width: int
    height: int
    renderer_name: str
    renderer_version: str
    warnings: tuple[str, ...]


async def materialize_export_visuals(
    *,
    export_ir: ExportIR,
    asset_resolution: SnapshotAssetResolution,
    attempt_dir: Path,
    browser_renderer: BrowserVisualRenderer,
) -> tuple[MaterializedVisual, ...]:
    if browser_renderer.version != EXPORT_VISUAL_RENDERER_VERSION:
        raise VisualMaterializationError("visual_renderer_version_mismatch")
    resolved = {asset.artifact_id: asset for asset in asset_resolution.resolved}
    if len(resolved) != len(asset_resolution.resolved):
        raise VisualMaterializationError("duplicate_resolved_asset")
    output_dir = _prepare_output_dir(attempt_dir)
    materialized: list[MaterializedVisual] = []
    for index, block in enumerate(iter_export_blocks(export_ir.blocks)):
        if block.visual_input_sha256 is None:
            continue
        expected_hashes = _expected_artifact_hashes(block)
        asset_paths = {
            artifact_id: _validate_resolved_asset(
                attempt_dir=attempt_dir,
                resolved=resolved.get(artifact_id),
                expected_hash=content_hash,
            )
            for artifact_id, content_hash in expected_hashes.items()
        }
        content, renderer_name, warnings = await _materialize_block(
            block=block,
            asset_paths=asset_paths,
            browser_renderer=browser_renderer,
        )
        png, width, height, pixel_sha256 = _normalize_png(content)
        _validate_expected_dimensions(
            block=block,
            width=width,
            height=height,
        )
        relative_path = _write_visual(
            output_dir=output_dir,
            index=index,
            block_key=block.block_key,
            content=png,
        )
        materialized.append(
            MaterializedVisual(
                block_key=block.block_key,
                section_id=block.source.section_id,
                section_rev=block.source.section_rev,
                section_kind=block.source.section_kind,
                visual_input_sha256=block.visual_input_sha256,
                source_artifact_hashes=tuple(sorted(expected_hashes.items())),
                relative_path=relative_path,
                mime="image/png",
                content_sha256=_sha256_bytes(png),
                pixel_sha256=pixel_sha256,
                width=width,
                height=height,
                renderer_name=renderer_name,
                renderer_version=browser_renderer.version,
                warnings=warnings,
            )
        )
    expected_count = sum(block.visual_input_sha256 is not None for block in iter_export_blocks(export_ir.blocks))
    if len(materialized) != expected_count:
        raise VisualMaterializationError("visual_materialization_accounting_mismatch")
    return tuple(materialized)


async def _materialize_block(
    *,
    block: ExportIRBlock,
    asset_paths: dict[str, Path],
    browser_renderer: BrowserVisualRenderer,
) -> tuple[bytes, str, tuple[str, ...]]:
    kind = block.source.section_kind
    payload = block.content.get("payload")
    if not isinstance(payload, dict):
        raise VisualMaterializationError("visual_payload_invalid")
    if kind == "image":
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise VisualMaterializationError("asset_missing_or_forbidden")
        return (
            _read_bounded(asset_paths.get(artifact_id)),
            "pinned-image-v1",
            (),
        )
    if kind == "design":
        flattened_id = payload.get("flattened_artifact_id")
        flattened_source_rev = payload.get("flattened_source_rev")
        if isinstance(flattened_id, str) and flattened_source_rev == block.source.section_rev - 1:
            return (
                _read_bounded(asset_paths.get(flattened_id)),
                "design-flatten-v2",
                (),
            )
        background = payload.get("background")
        background_id = background.get("artifact_id") if isinstance(background, dict) else None
        if not isinstance(background_id, str):
            raise VisualMaterializationError("asset_missing_or_forbidden")
        try:
            png, warnings = render_design_png(
                payload,
                _read_bounded(asset_paths.get(background_id)),
            )
        except DesignFlattenError as exc:
            raise VisualMaterializationError(
                "visual_materialization_failed",
                section_id=block.source.section_id,
            ) from exc
        return png, "design-flatten-v2", tuple(warnings)

    rendered = await browser_renderer.render(
        block,
        asset_paths=asset_paths,
    )
    if rendered.mime != "image/png" or rendered.visual_input_sha256 != block.visual_input_sha256:
        raise VisualMaterializationError(
            "visual_renderer_receipt_mismatch",
            section_id=block.source.section_id,
        )
    return rendered.content, browser_renderer.name, rendered.warnings


def _prepare_output_dir(attempt_dir: Path) -> Path:
    if not attempt_dir.is_absolute():
        raise VisualMaterializationError("visual_staging_invalid")
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if attempt_dir.is_symlink() or not attempt_dir.is_dir():
        raise VisualMaterializationError("visual_staging_invalid")
    output_dir = attempt_dir / "visuals"
    output_dir.mkdir(mode=0o700, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise VisualMaterializationError("visual_staging_invalid")
    return output_dir


def _expected_artifact_hashes(
    block: ExportIRBlock,
) -> dict[str, str]:
    raw = block.content.get("artifact_content_hashes")
    if not isinstance(raw, dict):
        return {}
    return {artifact_id: content_hash for artifact_id, content_hash in raw.items() if isinstance(artifact_id, str) and isinstance(content_hash, str)}


def _validate_resolved_asset(
    *,
    attempt_dir: Path,
    resolved: ResolvedSnapshotAsset | None,
    expected_hash: str,
) -> Path:
    if resolved is None or resolved.content_sha256 != expected_hash or resolved.relative_path is None:
        raise VisualMaterializationError("asset_missing_or_forbidden")
    relative = Path(resolved.relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise VisualMaterializationError("asset_staging_invalid")
    root = attempt_dir.resolve()
    path = attempt_dir / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise VisualMaterializationError("asset_staging_invalid")
    content = _read_bounded(path)
    if len(content) != resolved.size_bytes or _sha256_bytes(content) != resolved.content_sha256:
        raise VisualMaterializationError("asset_content_changed")
    return path


def _read_bounded(path: Path | None) -> bytes:
    if path is None:
        raise VisualMaterializationError("asset_missing_or_forbidden")
    size = path.stat().st_size
    if size <= 0 or size > MAX_VISUAL_BYTES:
        raise VisualMaterializationError("visual_size_invalid")
    return path.read_bytes()


def _normalize_png(content: bytes) -> tuple[bytes, int, int, str]:
    if not content or len(content) > MAX_VISUAL_BYTES:
        raise VisualMaterializationError("visual_size_invalid")
    try:
        with Image.open(io.BytesIO(content)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width > MAX_VISUAL_DIMENSION or height > MAX_VISUAL_DIMENSION or width * height > MAX_VISUAL_PIXELS:
                raise VisualMaterializationError("visual_dimensions_invalid")
            source.seek(0)
            transposed = ImageOps.exif_transpose(source)
            if transposed is None:
                raise VisualMaterializationError("visual_decode_failed")
            normalized = transposed.convert("RGBA")
            output = io.BytesIO()
            normalized.save(
                output,
                format="PNG",
                compress_level=6,
                optimize=False,
            )
    except (UnidentifiedImageError, OSError) as exc:
        raise VisualMaterializationError("visual_decode_failed") from exc
    png = output.getvalue()
    if len(png) > MAX_VISUAL_BYTES:
        raise VisualMaterializationError("visual_size_invalid")
    # PDF has an opaque white page and common writers flatten alpha while
    # preserving the visible RGB pixels.  Hash the pixels a reader actually
    # sees so the closed-file PDF extractor can compare the embedded image
    # without treating an equivalent alpha encoding as a mismatch.
    visible = Image.new("RGB", normalized.size, "white")
    visible.paste(normalized, mask=normalized.getchannel("A"))
    pixel_identity = visible.width.to_bytes(4, "big") + visible.height.to_bytes(4, "big") + visible.tobytes()
    return (
        png,
        normalized.width,
        normalized.height,
        _sha256_bytes(pixel_identity),
    )


def _validate_expected_dimensions(
    *,
    block: ExportIRBlock,
    width: int,
    height: int,
) -> None:
    kind = block.source.section_kind
    if kind in {"chart", "spec_block"}:
        if (width, height) != CHART_VISUAL_SIZE:
            raise VisualMaterializationError("visual_dimensions_invalid")
        return
    if kind != "design":
        return
    payload = block.content.get("payload")
    if not isinstance(payload, dict):
        raise VisualMaterializationError("visual_payload_invalid")
    expected_width = payload.get("canvas_width")
    expected_height = payload.get("canvas_height")
    if (width, height) != (expected_width, expected_height):
        raise VisualMaterializationError("visual_dimensions_invalid")


def _write_visual(
    *,
    output_dir: Path,
    index: int,
    block_key: str,
    content: bytes,
) -> str:
    block_digest = hashlib.sha256(block_key.encode("utf-8")).hexdigest()[:16]
    name = f"{index:04d}-{block_digest}.png"
    path = output_dir / name
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise VisualMaterializationError("visual_staging_conflict") from exc
    return (Path("visuals") / name).as_posix()


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


__all__ = [
    "BrowserRenderedVisual",
    "BrowserVisualRenderer",
    "CHART_VISUAL_SIZE",
    "MaterializedVisual",
    "VisualMaterializationError",
    "materialize_export_visuals",
]
