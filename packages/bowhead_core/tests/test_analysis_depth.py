"""Analysis-depth knob → internal budget profiles (fast/standard/thorough)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.agents.analysis_depth import (
    DEPTH_LABELS,
    get_analysis_depth,
    normalize_depth,
    reset_analysis_depth,
    resolve_peer_bail_iters,
    set_analysis_depth,
)

if TYPE_CHECKING:
    import pytest


def test_normalize_unknown_falls_to_standard() -> None:
    assert normalize_depth("深度") == "standard"  # only english keys are valid
    assert normalize_depth("thorough") == "thorough"
    assert normalize_depth("FAST") == "fast"  # case-insensitive
    assert normalize_depth(None) == "standard"
    assert normalize_depth("garbage") == "standard"


def test_labels_cover_all_depths() -> None:
    assert DEPTH_LABELS == {"fast": "快速", "standard": "标准", "thorough": "深度"}


def test_resolve_per_depth() -> None:
    tok = set_analysis_depth("fast")
    try:
        assert resolve_peer_bail_iters() == (14, 20)
    finally:
        reset_analysis_depth(tok)
    tok = set_analysis_depth("standard")
    try:
        assert resolve_peer_bail_iters() == (24, 32)
        assert get_analysis_depth() == "standard"
    finally:
        reset_analysis_depth(tok)
    tok = set_analysis_depth("thorough")
    try:
        assert resolve_peer_bail_iters() == (34, 46)
    finally:
        reset_analysis_depth(tok)


def test_default_is_standard() -> None:
    # no explicit set → the env-seeded default (standard, no env override here)
    assert resolve_peer_bail_iters() == (24, 32)


def test_explicit_env_override_wins_over_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PEER_HARD_BAIL_ITER", "9")
    monkeypatch.setenv("BOWHEAD_PEER_FORCE_BAIL_ITER", "11")
    tok = set_analysis_depth("thorough")  # would be (34,46) without the override
    try:
        assert resolve_peer_bail_iters() == (9, 11)  # ops escape hatch wins
    finally:
        reset_analysis_depth(tok)


def test_unknown_depth_value_resolves_standard() -> None:
    tok = set_analysis_depth("nonsense")  # normalized → standard
    try:
        assert resolve_peer_bail_iters() == (24, 32)
    finally:
        reset_analysis_depth(tok)


async def test_depth_propagates_to_child_task() -> None:
    """The exact mechanism chat.py relies on: set the depth, THEN create_task —
    the child (the turn, and its peer spawns) inherits the context copy."""
    import asyncio

    async def _child() -> tuple[int, int]:
        return resolve_peer_bail_iters()

    tok = set_analysis_depth("thorough")
    try:
        # task created AFTER the set → inherits the depth (copy-on-create)
        assert await asyncio.create_task(_child()) == (34, 46)
    finally:
        reset_analysis_depth(tok)
