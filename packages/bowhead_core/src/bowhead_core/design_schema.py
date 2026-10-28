"""Schema helpers for Multimodal V2A editable design sections."""

from __future__ import annotations

import math
import re
from typing import Any

DESIGN_SIZE_PRESETS: dict[str, tuple[int, int]] = {
    "square": (1024, 1024),
    "portrait_3_4": (1140, 1472),
    "portrait_9_16": (1080, 1920),
    "banner_16_9": (1600, 900),
}

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TEXT_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ALIGN_VALUES = {"left", "center", "right"}
FONT_FAMILY_VALUES = {"Noto Sans SC", "system"}


def design_size_metric_label(value: Any) -> str:
    raw = str(value or "portrait_3_4")
    return raw if raw in DESIGN_SIZE_PRESETS else "unknown"


def clean_design_text(value: Any, *, max_chars: int) -> str:
    return TEXT_CTRL_RE.sub("", str(value or "")).strip()[:max_chars]


def clamp_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    if not math.isfinite(v):
        v = default
    return min(max(v, minimum), maximum)


def clamp_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    return min(max(v, minimum), maximum)


def normalize_layer_id(value: Any, idx: int) -> str:
    raw = str(value or "").strip()
    if not raw or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", raw):
        return f"ly_{idx + 1}"
    return raw


def normalize_font_family(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw in FONT_FAMILY_VALUES else "Noto Sans SC"


def _validate_layers_input(raw_layers: Any) -> str | None:
    if not isinstance(raw_layers, list) or not raw_layers:
        return "layers must be a non-empty array"
    if len(raw_layers) > 12:
        return "layers maxItems is 12"
    return None


def _normalize_design_layer(raw: Any, idx: int) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, f"layers[{idx}] must be object"
    if str(raw.get("type") or "text") != "text":
        return None, f"layers[{idx}].type must be text"
    text = clean_design_text(raw.get("text"), max_chars=160)
    if not text:
        return None, f"layers[{idx}].text required"
    color = str(raw.get("color") or "#111111").strip()
    if not HEX_COLOR_RE.fullmatch(color):
        color = "#111111"
    align = str(raw.get("align") or "center").strip()
    if align not in ALIGN_VALUES:
        align = "center"
    return {
        "id": normalize_layer_id(raw.get("id"), idx),
        "type": "text",
        "text": text,
        "x": clamp_float(raw.get("x"), 0.1, minimum=0.0, maximum=1.0),
        "y": clamp_float(raw.get("y"), 0.1 + idx * 0.12, minimum=0.0, maximum=1.0),
        "w": clamp_float(raw.get("w"), 0.8, minimum=0.01, maximum=1.0),
        "h": clamp_float(raw.get("h"), 0.12, minimum=0.01, maximum=1.0),
        "font_family": normalize_font_family(raw.get("font_family")),
        "font_size": clamp_int(raw.get("font_size"), 56, minimum=8, maximum=160),
        "font_weight": clamp_int(raw.get("font_weight"), 700, minimum=300, maximum=900),
        "color": color,
        "align": align,
        "line_height": clamp_float(raw.get("line_height"), 1.08, minimum=0.9, maximum=1.8),
        "letter_spacing": clamp_float(raw.get("letter_spacing"), 0.0, minimum=-2.0, maximum=8.0),
        "rotation": 0,
        "z": clamp_int(raw.get("z"), idx * 10, minimum=-1000, maximum=1000),
        "locked": bool(raw.get("locked", False)),
    }, None


def normalize_design_layers(raw_layers: Any) -> tuple[list[dict[str, Any]], str | None]:
    input_error = _validate_layers_input(raw_layers)
    if input_error:
        return [], input_error

    seen: set[str] = set()
    layers: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_layers):
        layer, layer_error = _normalize_design_layer(raw, idx)
        if layer_error or layer is None:
            return [], layer_error
        layer_id = str(layer["id"])
        if layer_id in seen:
            return [], f"duplicate layer id {layer_id!r}"
        seen.add(layer_id)
        layers.append(layer)
    return layers, None


def normalize_design_payload(
    *,
    title: Any,
    size_preset: Any,
    background_artifact_id: str,
    layers: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    normalized_layers, layer_error = normalize_design_layers(layers)
    if layer_error:
        return None, layer_error
    preset = str(size_preset or "portrait_3_4")
    if preset not in DESIGN_SIZE_PRESETS:
        preset = "portrait_3_4"
    width, height = DESIGN_SIZE_PRESETS[preset]
    cleaned_title = clean_design_text(title or "可编辑设计稿", max_chars=60)
    return {
        "title": cleaned_title or "可编辑设计稿",
        "size_preset": preset,
        "canvas_width": width,
        "canvas_height": height,
        "background": {
            "type": "artifact",
            "artifact_id": background_artifact_id,
            "fit": "cover",
            "opacity": 1,
        },
        "layers": normalized_layers,
        "flattened_artifact_id": None,
        "flattened_source_rev": None,
        "flattened_at": None,
    }, None


def design_text_layer_summary(payload: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for layer in payload.get("layers") or []:
        if isinstance(layer, dict) and layer.get("type") == "text":
            out.append(
                {
                    "layer_id": str(layer.get("id") or ""),
                    "text": str(layer.get("text") or ""),
                }
            )
    return out
