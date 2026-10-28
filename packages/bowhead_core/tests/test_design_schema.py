"""Editable design schema normalization tests."""

from __future__ import annotations

import math

from bowhead_core.design_schema import (
    clean_design_text,
    design_size_metric_label,
    design_text_layer_summary,
    normalize_design_layers,
    normalize_design_payload,
)


def test_clean_design_text_strips_control_chars_and_truncates() -> None:
    assert clean_design_text("\x00 夏日冰爽 \x7f", max_chars=3) == "夏日冰"


def test_design_size_metric_label_uses_bounded_values() -> None:
    assert design_size_metric_label("portrait_3_4") == "portrait_3_4"
    assert design_size_metric_label("custom_999") == "unknown"


def test_normalize_design_payload_defaults_size_and_title() -> None:
    payload, error = normalize_design_payload(
        title="\x00  ",
        size_preset="custom_size",
        background_artifact_id="art_123456789abc",
        layers=[{"text": "夏日新品", "x": 0.1, "y": 0.2, "w": 0.8, "h": 0.1}],
    )

    assert error is None
    assert payload is not None
    assert payload["title"] == "可编辑设计稿"
    assert payload["size_preset"] == "portrait_3_4"
    assert payload["canvas_width"] == 1140
    assert payload["canvas_height"] == 1472
    assert payload["background"]["artifact_id"] == "art_123456789abc"
    assert payload["flattened_artifact_id"] is None


def test_normalize_design_layers_clamps_and_defaults_fields() -> None:
    layers, error = normalize_design_layers(
        [
            {
                "id": "<bad id>",
                "type": "text",
                "text": "  第二杯半价  ",
                "x": -1,
                "y": math.nan,
                "w": 0,
                "h": 3,
                "font_family": "Bad Font",
                "font_size": 999,
                "font_weight": 1200,
                "color": "red",
                "align": "middle",
                "line_height": 9,
                "letter_spacing": -9,
                "rotation": 15,
                "z": -2000,
            },
        ]
    )

    assert error is None
    assert layers == [
        {
            "id": "ly_1",
            "type": "text",
            "text": "第二杯半价",
            "x": 0.0,
            "y": 0.1,
            "w": 0.01,
            "h": 1.0,
            "font_family": "Noto Sans SC",
            "font_size": 160,
            "font_weight": 900,
            "color": "#111111",
            "align": "center",
            "line_height": 1.8,
            "letter_spacing": -2.0,
            "rotation": 0,
            "z": -1000,
            "locked": False,
        },
    ]


def test_normalize_design_layers_preserves_allowed_style_fields() -> None:
    layers, error = normalize_design_layers(
        [
            {
                "id": "cta",
                "text": "立即抢购",
                "x": 0.2,
                "y": 0.7,
                "w": 0.6,
                "h": 0.08,
                "font_family": "system",
                "font_size": 48,
                "font_weight": 600,
                "color": "#FFAA00",
                "align": "right",
                "line_height": 1.2,
                "letter_spacing": 1.5,
                "z": 20,
                "locked": True,
            },
        ]
    )

    assert error is None
    assert layers[0]["id"] == "cta"
    assert layers[0]["font_family"] == "system"
    assert layers[0]["font_weight"] == 600
    assert layers[0]["color"] == "#FFAA00"
    assert layers[0]["align"] == "right"
    assert layers[0]["line_height"] == 1.2
    assert layers[0]["letter_spacing"] == 1.5
    assert layers[0]["locked"] is True


def test_normalize_design_layers_rejects_invalid_shapes() -> None:
    assert normalize_design_layers([])[1] == "layers must be a non-empty array"
    assert normalize_design_layers([{"text": str(i)} for i in range(13)])[1] == "layers maxItems is 12"
    assert normalize_design_layers(["bad"])[1] == "layers[0] must be object"
    assert normalize_design_layers([{"type": "image", "text": "x"}])[1] == "layers[0].type must be text"
    assert normalize_design_layers([{"text": "\x00"}])[1] == "layers[0].text required"
    assert normalize_design_layers([{"id": "dup", "text": "A"}, {"id": "dup", "text": "B"}])[1] == ("duplicate layer id 'dup'")


def test_design_text_layer_summary_filters_non_text_layers() -> None:
    assert design_text_layer_summary(
        {
            "layers": [
                {"id": "a", "type": "text", "text": "A"},
                {"id": "b", "type": "image", "text": "B"},
                "bad",
            ],
        }
    ) == [{"layer_id": "a", "text": "A"}]
