"""Schema-driven renderer for Multimodal V2A editable design sections."""

from __future__ import annotations

import io
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFont

from bowhead_core.canvas_store import CanvasRevConflictError

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactStore
    from bowhead_core.canvas_store import CanvasStore, Section

_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlattenDesignResult:
    artifact_id: str
    section: dict[str, Any]
    rev: int
    warnings: list[str]


class DesignFlattenError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        current_rev: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.current_rev = current_rev


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _backend_label(artifact_store: ArtifactStore) -> str:
    raw = str(getattr(getattr(artifact_store, "backend", None), "name", "") or "unknown")
    return raw if raw in {"local", "s3"} else "other"


def _metric_inc(name: str, labels: dict[str, str] | None = None) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(name, labels=labels or {})
    except Exception:
        return


def _metric_observe(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.observe(name, value, labels=labels or {})
    except Exception:
        return


async def _soft_delete_artifact_best_effort(
    artifact_store: ArtifactStore,
    artifact_id: str,
    *,
    user_id: str,
) -> None:
    try:
        await artifact_store.soft_delete(artifact_id, user_id=user_id)
    except Exception as exc:
        logger.warning("design_flatten.cleanup_failed art=%s err=%s", artifact_id, exc)


def _repo_font_path() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "fonts" / "NotoSansSC-Regular.otf"


def _font_candidates() -> list[Path]:
    env = os.environ.get("BOWHEAD_DESIGN_FONT_NOTO_SANS_SC")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.extend(
        [
            _repo_font_path(),
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/System/Library/Fonts/STHeiti Light.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        ]
    )
    return candidates


def _load_font(size: int) -> tuple[Any, str | None]:
    for path in _font_candidates():
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size), None
        except Exception as exc:
            logger.debug("design_flatten.font_load_failed path=%s err=%s", path, exc)
            continue
    if os.environ.get("BOWHEAD_DESIGN_FONT_ALLOW_FALLBACK") == "1":
        return ImageFont.load_default(size=size), "font_fallback"
    raise DesignFlattenError(
        "font_missing",
        "design flatten CJK font not configured",
        status_code=503,
    )


def _clamp_number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    if not isfinite(n):
        return fallback
    return max(minimum, min(maximum, n))


def _parse_color(value: Any) -> tuple[int, int, int, int]:
    raw = str(value or "#111111").strip()
    if not _HEX_COLOR_RE.fullmatch(raw):
        raw = "#111111"
    return (
        int(raw[1:3], 16),
        int(raw[3:5], 16),
        int(raw[5:7], 16),
        255,
    )


def _fit_background(img: Any, width: int, height: int, fit: str) -> Any:
    src = img.convert("RGBA")
    if fit == "fill":
        return src.resize((width, height), Image.Resampling.LANCZOS)
    if fit == "contain":
        scale = min(width / src.width, height / src.height)
        resized = src.resize(
            (max(1, round(src.width * scale)), max(1, round(src.height * scale))),
            Image.Resampling.LANCZOS,
        )
        out = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        out.alpha_composite(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
        return out
    scale = max(width / src.width, height / src.height)
    resized = src.resize(
        (max(1, round(src.width * scale)), max(1, round(src.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _apply_opacity(img: Any, opacity: float) -> Any:
    if opacity >= 0.999:
        return img
    out = img.copy()
    alpha = out.getchannel("A").point(lambda p: int(p * opacity))
    out.putalpha(alpha)
    return out


def _text_width(draw: Any, text: str, font: Any, letter_spacing: float = 0.0) -> float:
    if letter_spacing and len(text) > 1:
        return sum(_text_width(draw, ch, font) for ch in text) + letter_spacing * (len(text) - 1)
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return float(bbox[2] - bbox[0])


def _wrap_line(draw: Any, line: str, font: Any, max_width: int, letter_spacing: float) -> list[str]:
    if not line:
        return [""]
    out: list[str] = []
    cur = ""
    for ch in line:
        candidate = cur + ch
        if not cur or _text_width(draw, candidate, font, letter_spacing) <= max_width:
            cur = candidate
            continue
        out.append(cur)
        cur = ch
    if cur:
        out.append(cur)
    return out


def _draw_text(
    draw: Any,
    xy: tuple[int, int],
    text: str,
    *,
    font: Any,
    fill: tuple[int, int, int, int],
    letter_spacing: float,
    stroke_width: int = 0,
) -> None:
    if not text or not letter_spacing:
        draw.text(
            xy,
            text,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=fill,
        )
        return
    x, y = xy
    for ch in text:
        draw.text(
            (x, y),
            ch,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=fill,
        )
        x += round(_text_width(draw, ch, font) + letter_spacing)


def _text_top(draw: Any, text: str, font: Any, stroke_width: int) -> int:
    try:
        return int(draw.textbbox((0, 0), text or " ", font=font, stroke_width=stroke_width)[1])
    except Exception:
        return 0


def _draw_text_layer(
    base: Any,
    *,
    layer: dict[str, Any],
    canvas_width: int,
    canvas_height: int,
    warnings: list[str],
) -> None:
    box_x = round(_clamp_number(layer.get("x"), 0, 0, 1) * canvas_width)
    box_y = round(_clamp_number(layer.get("y"), 0, 0, 1) * canvas_height)
    box_w = max(1, round(_clamp_number(layer.get("w"), 0.8, 0.01, 1) * canvas_width))
    box_h = max(1, round(_clamp_number(layer.get("h"), 0.12, 0.01, 1) * canvas_height))
    font_size = round(_clamp_number(layer.get("font_size"), 56, 8, 160))
    font, font_warning = _load_font(font_size)
    if font_warning and font_warning not in warnings:
        warnings.append(font_warning)
    font_weight = round(_clamp_number(layer.get("font_weight"), 700, 300, 900))
    stroke_width = 2 if font_weight >= 850 else 1 if font_weight >= 700 else 0
    text = str(layer.get("text") or "")
    align = str(layer.get("align") or "center")
    if align not in {"left", "center", "right"}:
        align = "center"
    line_height = max(1, round(font_size * _clamp_number(layer.get("line_height"), 1.08, 0.9, 1.8)))
    letter_spacing = _clamp_number(layer.get("letter_spacing"), 0.0, -2.0, 8.0)

    layer_img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer_img)
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        lines.extend(_wrap_line(draw, raw_line, font, box_w, letter_spacing))

    y = 0
    overflow = False
    for line in lines:
        if y >= box_h:
            overflow = True
            break
        text_w = _text_width(draw, line, font, letter_spacing)
        if align == "left":
            x = 0
        elif align == "right":
            x = max(0, box_w - round(text_w))
        else:
            x = max(0, (box_w - round(text_w)) // 2)
        text_y = y - _text_top(draw, line, font, stroke_width)
        _draw_text(
            draw,
            (x + 1, text_y + 1),
            line,
            font=font,
            fill=(0, 0, 0, 70),
            letter_spacing=letter_spacing,
            stroke_width=0,
        )
        _draw_text(
            draw,
            (x, text_y),
            line,
            font=font,
            fill=_parse_color(layer.get("color")),
            letter_spacing=letter_spacing,
            stroke_width=stroke_width,
        )
        y += line_height
        if y > box_h:
            overflow = True
    if overflow:
        warnings.append(f"text_overflow:{layer.get('id') or ''}")
    base.alpha_composite(layer_img, (box_x, box_y))


def render_design_png(
    payload: dict[str, Any],
    background_bytes: bytes,
) -> tuple[bytes, list[str]]:
    width = max(1, round(_clamp_number(payload.get("canvas_width"), 1080, 1, 4096)))
    height = max(1, round(_clamp_number(payload.get("canvas_height"), 1440, 1, 4096)))
    try:
        bg_img = Image.open(io.BytesIO(background_bytes))
    except Exception as exc:
        raise DesignFlattenError(
            "background_decode_failed",
            "background image could not be decoded",
            status_code=400,
        ) from exc
    background = payload.get("background") if isinstance(payload.get("background"), dict) else {}
    fit = str(background.get("fit") or "cover")
    opacity = _clamp_number(background.get("opacity"), 1, 0, 1)
    base = _apply_opacity(_fit_background(bg_img, width, height, fit), opacity)
    warnings: list[str] = []
    raw_layers = payload.get("layers")
    layers = raw_layers if isinstance(raw_layers, list) else []
    indexed_layers = [(idx, layer) for idx, layer in enumerate(layers) if isinstance(layer, dict) and layer.get("type", "text") == "text"]
    indexed_layers.sort(
        key=lambda item: (
            _clamp_number(item[1].get("z"), item[0], -1000, 1000),
            item[0],
        ),
    )
    for _, layer in indexed_layers:
        _draw_text_layer(
            base,
            layer=layer,
            canvas_width=width,
            canvas_height=height,
            warnings=warnings,
        )
    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue(), warnings


def _render_design_png(
    payload: dict[str, Any],
    background_bytes: bytes,
) -> tuple[bytes, list[str]]:
    """Compatibility seam for existing flatten fault-injection tests."""
    return render_design_png(payload, background_bytes)


async def flatten_design_section(
    *,
    artifact_store: ArtifactStore,
    canvas_store: CanvasStore,
    canvas_id: str,
    section_id: str,
    user_id: str,
    session_id: str | None,
    expected_rev: int | None = None,
    created_by_turn: str | None = None,
) -> FlattenDesignResult:
    started = time.perf_counter()
    backend = _backend_label(artifact_store)
    _metric_inc("bowhead_design_flatten_total", {"status": "started", "backend": backend})
    try:
        result = await _flatten_design_section_impl(
            artifact_store=artifact_store,
            canvas_store=canvas_store,
            canvas_id=canvas_id,
            section_id=section_id,
            user_id=user_id,
            session_id=session_id,
            expected_rev=expected_rev,
            created_by_turn=created_by_turn,
        )
    except DesignFlattenError as exc:
        _metric_inc("bowhead_design_flatten_total", {"status": exc.code, "backend": backend})
        if exc.code == "font_missing":
            logger.warning(
                "design_flatten.font_missing canvas=%s section=%s",
                canvas_id,
                section_id,
            )
        raise
    except Exception:
        _metric_inc("bowhead_design_flatten_total", {"status": "error", "backend": backend})
        raise
    finally:
        duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        _metric_observe("bowhead_design_flatten_duration_ms", duration_ms, {"backend": backend})
    _metric_inc("bowhead_design_flatten_total", {"status": "done", "backend": backend})
    return result


async def _flatten_design_section_impl(
    *,
    artifact_store: ArtifactStore,
    canvas_store: CanvasStore,
    canvas_id: str,
    section_id: str,
    user_id: str,
    session_id: str | None,
    expected_rev: int | None = None,
    created_by_turn: str | None = None,
) -> FlattenDesignResult:
    sections = await canvas_store.list_sections(canvas_id, include_hidden=True)
    section: Section | None = next((s for s in sections if s.id == section_id), None)
    if section is None:
        raise DesignFlattenError("section_not_found", "section not found", status_code=404)
    if section.kind != "design":
        raise DesignFlattenError("not_design", "section is not a design", status_code=400)
    if expected_rev is not None and section.rev != expected_rev:
        raise DesignFlattenError(
            "rev_conflict",
            "section revision conflict",
            status_code=409,
            current_rev=section.rev,
        )
    payload = dict(section.payload or {})
    background = payload.get("background")
    if not isinstance(background, dict):
        raise DesignFlattenError("background_missing", "design background missing", status_code=400)
    background_artifact_id = str(background.get("artifact_id") or "").strip()
    if not background_artifact_id:
        raise DesignFlattenError(
            "background_missing",
            "design background artifact missing",
            status_code=400,
        )
    background_art = await artifact_store.get(
        background_artifact_id,
        check_created_by=user_id,
    )
    if background_art is None:
        raise DesignFlattenError(
            "background_not_found",
            "background artifact not found",
            status_code=404,
        )
    if (background_art.mime or "").lower() not in _IMAGE_MIMES:
        raise DesignFlattenError(
            "background_not_image",
            "background artifact is not an image",
            status_code=400,
        )
    background_bytes = await artifact_store.open_read(background_art)
    source_rev = section.rev
    png_bytes, warnings = _render_design_png(payload, background_bytes)
    flattened_at = _now()
    art = await artifact_store.put_bytes(
        content=png_bytes,
        filename=f"design_{section_id}_r{source_rev}.png",
        mime="image/png",
        source="design_flatten",
        canvas_id=canvas_id,
        created_by=user_id,
        session_id=session_id,
        kind="deliverable",
        inject="stub",
        producer_kind="design_flatten",
        created_by_turn=created_by_turn,
        derived_from=background_artifact_id,
    )
    await artifact_store.put_design_flatten_provenance(
        artifact_id=art.id,
        canvas_id=canvas_id,
        section_id=section_id,
        section_rev=source_rev,
        size_preset=str(payload.get("size_preset") or ""),
        background_artifact_id=background_artifact_id,
        created_at=flattened_at,
    )
    try:
        updated = await canvas_store.patch_section(
            section_id=section_id,
            patch={
                "flattened_artifact_id": art.id,
                "flattened_source_rev": source_rev,
                "flattened_at": flattened_at,
            },
            expected_rev=source_rev,
            by_turn_id=created_by_turn,
        )
    except CanvasRevConflictError as exc:
        await _soft_delete_artifact_best_effort(artifact_store, art.id, user_id=user_id)
        raise DesignFlattenError(
            "rev_conflict",
            "section revision conflict",
            status_code=409,
            current_rev=exc.current_rev,
        ) from exc
    if updated is None:
        await _soft_delete_artifact_best_effort(artifact_store, art.id, user_id=user_id)
        raise DesignFlattenError("section_not_found", "section not found", status_code=404)
    return FlattenDesignResult(
        artifact_id=art.id,
        section=updated.to_report_section(),
        rev=updated.rev,
        warnings=warnings,
    )
