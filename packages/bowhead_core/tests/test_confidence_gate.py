"""Tests for sprint δ-#3 — confidence_gate.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.confidence_gate import (
    DEFAULT_THRESHOLD,
    ConfidenceJudgment,
    gate,
    load_threshold_for_model,
    reset_threshold_cache,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _j(conf: int) -> ConfidenceJudgment:
    return ConfidenceJudgment(claim="EBITDA = 营收-COGS", proposed_source="[llm-std:]", confidence=conf)


# ─── gate decisions ────────────────────────────────────────


def test_gate_high_conf_silent() -> None:
    assert gate(_j(10)) == "use_silent"
    assert gate(_j(9)) == "use_silent"


def test_gate_mid_conf_surface() -> None:
    """7 ≤ conf < 9 → use_with_surface (banner)."""
    assert gate(_j(8)) == "use_with_surface"
    assert gate(_j(7)) == "use_with_surface"


def test_gate_low_conf_hitl() -> None:
    """conf < threshold → trigger_hitl."""
    assert gate(_j(6)) == "trigger_hitl"
    assert gate(_j(0)) == "trigger_hitl"


def test_gate_clamp_above_10() -> None:
    """Defensive: out-of-range conf clamped to [0, 10]."""
    assert gate(_j(15)) == "use_silent"
    assert gate(_j(-3)) == "trigger_hitl"


def test_gate_per_model_threshold_qwen() -> None:
    """Qwen-style threshold=8: conf=7 → trigger_hitl (under-confident model)."""
    assert gate(_j(7), threshold=8) == "trigger_hitl"
    assert gate(_j(8), threshold=8) == "use_with_surface"


def test_gate_per_model_threshold_doubao() -> None:
    """Doubao-style threshold=6: conf=6 → use_with_surface (over-confident model)."""
    assert gate(_j(6), threshold=6) == "use_with_surface"
    assert gate(_j(5), threshold=6) == "trigger_hitl"


def test_gate_silent_overrides_threshold() -> None:
    """Silent threshold is fixed at 9, never overridden by per-model threshold."""
    assert gate(_j(9), threshold=8) == "use_silent"
    assert gate(_j(9), threshold=6) == "use_silent"


# ─── load_threshold_for_model ──────────────────────────────


def test_threshold_default_when_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_CONFIDENCE_THRESHOLDS_PATH", str(tmp_path / "missing.yaml"))
    reset_threshold_cache()
    assert load_threshold_for_model("deepseek-v4-pro") == DEFAULT_THRESHOLD


def test_threshold_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "th.yaml"
    cfg.write_text(
        "deepseek-v4-pro: 7\ndoubao-seed-2.0: 6\nqwen3.6: 8\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOWHEAD_CONFIDENCE_THRESHOLDS_PATH", str(cfg))
    reset_threshold_cache()
    assert load_threshold_for_model("deepseek-v4-pro") == 7
    assert load_threshold_for_model("doubao-seed-2.0") == 6
    assert load_threshold_for_model("qwen3.6") == 8


def test_threshold_unknown_model_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "th.yaml"
    cfg.write_text("deepseek-v4-pro: 7\n", encoding="utf-8")
    monkeypatch.setenv("BOWHEAD_CONFIDENCE_THRESHOLDS_PATH", str(cfg))
    reset_threshold_cache()
    assert load_threshold_for_model("unknown-model-xyz") == DEFAULT_THRESHOLD


def test_threshold_yaml_clamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Out-of-range YAML values dropped, model falls back to default."""
    cfg = tmp_path / "th.yaml"
    cfg.write_text(
        "good-model: 8\nbad-model: 99\n",  # 99 > 10 → dropped
        encoding="utf-8",
    )
    monkeypatch.setenv("BOWHEAD_CONFIDENCE_THRESHOLDS_PATH", str(cfg))
    reset_threshold_cache()
    assert load_threshold_for_model("good-model") == 8
    assert load_threshold_for_model("bad-model") == DEFAULT_THRESHOLD
