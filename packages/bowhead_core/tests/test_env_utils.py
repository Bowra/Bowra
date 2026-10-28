"""env_utils.env_float — 统一 5 处 env-float-timeout 拷贝后的合同测试."""

from __future__ import annotations

import pytest
from bowhead_core.env_utils import env_float


@pytest.mark.unit
def test_unset_returns_default(monkeypatch):
    monkeypatch.delenv("BH_TEST_EF", raising=False)
    assert env_float("BH_TEST_EF", 18.0) == 18.0


@pytest.mark.unit
def test_empty_returns_default(monkeypatch):
    monkeypatch.setenv("BH_TEST_EF", "")
    assert env_float("BH_TEST_EF", 40.0) == 40.0


@pytest.mark.unit
def test_valid_parses(monkeypatch):
    monkeypatch.setenv("BH_TEST_EF", "12.5")
    assert env_float("BH_TEST_EF", 40.0) == 12.5


@pytest.mark.unit
def test_garbage_returns_default(monkeypatch):
    monkeypatch.setenv("BH_TEST_EF", "not-a-number")
    assert env_float("BH_TEST_EF", 40.0) == 40.0


@pytest.mark.unit
def test_nonpositive_clamped_to_default(monkeypatch):
    # Timeout/budget semantics: wait_for(0/negative) times out immediately, never intended → use the default.
    monkeypatch.setenv("BH_TEST_EF", "-5")
    assert env_float("BH_TEST_EF", 40.0) == 40.0
    monkeypatch.setenv("BH_TEST_EF", "0")
    assert env_float("BH_TEST_EF", 40.0) == 40.0


@pytest.mark.unit
def test_nan_clamped_to_default(monkeypatch):
    # Actual bug caught in review: float("nan") parses successfully, and nan<=0 is False →
    # The old `if v <= 0` leaked nan to asyncio.wait_for (immediate timeout + downstream int(nan) crash).
    # Clamping must use `not (v > 0)`.
    monkeypatch.setenv("BH_TEST_EF", "nan")
    assert env_float("BH_TEST_EF", 40.0) == 40.0


@pytest.mark.unit
def test_inf_passes_through(monkeypatch):
    # inf explicitly disables the cap, matching pre-unification float() passthrough at all sites (wait_for(inf)=no timeout).
    monkeypatch.setenv("BH_TEST_EF", "inf")
    assert env_float("BH_TEST_EF", 40.0) == float("inf")
