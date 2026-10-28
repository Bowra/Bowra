"""Confidence Gate — Sprint δ.

Decides whether to use LLM output silently, surface a confirmation banner, or
pause for HITL based on a confidence judgment emitted by the judge LLM after
subagent_return.

Per tech-design §1.4:
- conf ≥ 9: use_silent (no UI surface, write fact silently if applicable)
- threshold ≤ conf < 9: use_with_surface (show banner "我用了 X, 你可改")
- conf < threshold: trigger_hitl (pause turn, formula_confirm HITL)

Default threshold = 7. Per-model thresholds in
``config/confidence_thresholds.yaml`` (G3 per-model gate, round 3 PM-P0-4
bounded). Calibration tuning happens in δ-#8 eval (Doubao tends over-confident
→ may need 6, Qwen tends under-confident → 8).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Action = Literal["use_silent", "use_with_surface", "trigger_hitl"]

# Hard-coded fallback when YAML missing / parse fails. Spec default = 7.
DEFAULT_THRESHOLD = 7
SILENT_THRESHOLD = 9  # conf ≥ 9 → silent regardless of per-model threshold


@dataclass
class ConfidenceJudgment:
    """LLM judge output for one claim. Emitted post-subagent_return.

    Fields:
        claim: the claim text being assessed (for logging / HITL surface)
        proposed_source: BNF source spec the LLM picked, e.g. "[llm-std:]" or
            "[llm-rsn:bi:art_001|fact:cogs]"
        confidence: 0-10 integer self-rating
        concern: optional reasoning ("客户特殊点疑虑"), surfaced to user on HITL
    """

    claim: str
    proposed_source: str
    confidence: int
    concern: str | None = None


def gate(judgment: ConfidenceJudgment, threshold: int = DEFAULT_THRESHOLD) -> Action:
    """Decide action for a single claim's confidence judgment.

    - conf ≥ 9 → use_silent (high enough to not bother user)
    - threshold ≤ conf < 9 → use_with_surface (banner override available)
    - conf < threshold → trigger_hitl (pause for formula_confirm)

    Threshold is per-model (DeepSeek 7, Doubao maybe 6, Qwen maybe 8). Caller
    looks up via ``load_threshold_for_model`` then passes here.
    """
    conf = max(0, min(10, judgment.confidence))  # clamp defensive
    if conf >= SILENT_THRESHOLD:
        return "use_silent"
    if conf >= threshold:
        return "use_with_surface"
    return "trigger_hitl"


# YAML loader is best-effort: missing file or parse error → DEFAULT_THRESHOLD.
# Cached on first read; ``reset_threshold_cache`` for tests.
_THRESHOLD_CACHE: dict[str, int] | None = None


def _config_path() -> Path:
    env = os.getenv("BOWHEAD_CONFIDENCE_THRESHOLDS_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "config" / "confidence_thresholds.yaml"


def _load_thresholds() -> dict[str, int]:
    """Read YAML once; on any failure log + return empty dict (fall through to default)."""
    path = _config_path()
    if not path.exists():
        return {}
    try:
        import yaml  # local import — yaml only required when config present
    except ImportError:
        logger.warning("delta.confidence_yaml_skip reason=pyyaml_missing")
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as e:
        logger.warning("delta.confidence_yaml_load_failed err=%s", e)
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items() if isinstance(raw, dict) else []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= n <= 10:
            out[str(k)] = n
    return out


def load_threshold_for_model(model: str) -> int:
    """Look up threshold for a model name; falls back to ``DEFAULT_THRESHOLD``."""
    global _THRESHOLD_CACHE
    if _THRESHOLD_CACHE is None:
        _THRESHOLD_CACHE = _load_thresholds()
    return _THRESHOLD_CACHE.get(model, DEFAULT_THRESHOLD)


def reset_threshold_cache() -> None:
    """Test hook: drop cached YAML so next call re-reads from disk / env."""
    global _THRESHOLD_CACHE
    _THRESHOLD_CACHE = None
