"""Tests for sprint η — state.py module.

Covers:
  - HOT/WARM/COLD state TypedDict schema
  - push_recent_context: rolling 5 + LRU evict on cap overflow
  - serialize_hot / deserialize_hot round-trip
  - is_state_lifecycle_enabled: η flag（2026-07-03 rename，原 is_progressive_load_enabled）
  - HotStateOverflowError on pinned-alone overflow
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from bowhead_core.state import (
    _HOT_CAP_BYTES,
    HitlState,
    HotStateOverflowError,
    PendingAction,
    _default_state,
    _hot_size_bytes,
    deserialize_hot,
    is_state_lifecycle_enabled,
    push_recent_context,
    serialize_hot,
)

# ─── _default_state ─────────────────────────────────────────


def test_default_state_has_all_fields() -> None:
    s = _default_state()
    for k in (
        "current_user_message",
        "active_hitl",
        "pending_actions",
        "recent_context",
        "files_summary",
        "tasks_summary",
        "findings_summary",
    ):
        assert k in s


def test_default_state_under_cap() -> None:
    s = _default_state()
    assert _hot_size_bytes(s) < _HOT_CAP_BYTES


# ─── push_recent_context ────────────────────────────────────


def test_push_basic() -> None:
    s = _default_state()
    push_recent_context(s, "first")
    assert s["recent_context"] == ["first"]


def test_push_truncates_long_entry() -> None:
    s = _default_state()
    long = "x" * 2000
    push_recent_context(s, long)
    assert len(s["recent_context"][0]) <= 800


def test_push_rolling_window_max_5() -> None:
    s = _default_state()
    for i in range(7):
        push_recent_context(s, f"entry-{i}")
    # Only last 5 retained
    assert len(s["recent_context"]) == 5
    assert s["recent_context"][0] == "entry-2"
    assert s["recent_context"][-1] == "entry-6"


def test_push_lru_evict_on_cap_overflow() -> None:
    """When entries push HOT total over 5120 B, oldest evicted before raising."""
    s = _default_state()
    # Each entry ~700 bytes; pushing 5 of these brings recent_context to ~3.5 KB
    # Then pad pinned field to push toward cap
    s["current_user_message"] = "A" * 1500  # 1.5 KB pinned
    for _i in range(5):
        push_recent_context(s, "B" * 700)
    # All 5 entries kept (just at cap)
    assert _hot_size_bytes(s) <= _HOT_CAP_BYTES
    # Push 1 more big entry → triggers evict
    push_recent_context(s, "C" * 700)
    # Still ≤ 5; evict happened (oldest removed via cap loop AND rolling-5 cap)
    assert len(s["recent_context"]) <= 5
    assert _hot_size_bytes(s) <= _HOT_CAP_BYTES


def test_push_pinned_alone_overflow_raises() -> None:
    """If pinned fields alone > 5120, evict can't help — raise."""
    s = _default_state()
    s["current_user_message"] = "X" * 6000  # 6 KB pinned alone
    with pytest.raises(HotStateOverflowError):
        push_recent_context(s, "trigger")


# ─── serialize_hot / deserialize_hot ────────────────────────


def test_serialize_round_trip_empty() -> None:
    s = _default_state()
    payload = serialize_hot(s)
    s2 = deserialize_hot(payload)
    assert s2["current_user_message"] == ""
    assert s2["active_hitl"] is None
    assert s2["pending_actions"] == []
    assert s2["recent_context"] == []


def test_serialize_round_trip_with_hitl() -> None:
    s = _default_state()
    s["active_hitl"] = HitlState(
        clarification_id="c_001",
        kind="options",
        prompt="Pick one",
        options=["a", "b"],
        metadata={"foo": "bar"},
    )
    payload = serialize_hot(s)
    s2 = deserialize_hot(payload)
    h = s2["active_hitl"]
    assert h is not None
    assert h.clarification_id == "c_001"
    assert h.kind == "options"
    assert h.options == ["a", "b"]


def test_serialize_round_trip_with_pending_actions() -> None:
    s = _default_state()
    s["pending_actions"] = [
        PendingAction(action_type="add_chart", payload={"data": [1, 2]}, submitted_at="2026-05-08T01:00:00Z"),
    ]
    payload = serialize_hot(s)
    s2 = deserialize_hot(payload)
    assert len(s2["pending_actions"]) == 1
    assert s2["pending_actions"][0].action_type == "add_chart"


def test_deserialize_none_returns_default() -> None:
    s = deserialize_hot(None)
    assert s["current_user_message"] == ""


def test_deserialize_invalid_json_returns_default() -> None:
    s = deserialize_hot("not valid json {")
    assert s["current_user_message"] == ""


def test_serialize_does_not_include_warm() -> None:
    """serialize_hot only persists HOT (4 fields), WARM stays in PG-driven hydrate path."""
    s = _default_state()
    s["files_summary"] = "should-not-persist"
    payload = serialize_hot(s)
    assert "files_summary" not in payload
    assert "should-not-persist" not in payload


# ─── is_state_lifecycle_enabled (η flag, default ON) ────────


def test_pl_default_on() -> None:
    """η-#9 ship 0.8.4: default ON (env unset → True)."""
    with mock.patch.dict(os.environ, {}, clear=True):
        os.environ.pop("BOWHEAD_HOT_WARM_COLD_ENABLED", None)
        assert is_state_lifecycle_enabled() is True


def test_pl_explicit_true_via_env() -> None:
    for v in ("true", "True", "TRUE", "1", "yes", "on"):
        with mock.patch.dict(os.environ, {"BOWHEAD_HOT_WARM_COLD_ENABLED": v}):
            assert is_state_lifecycle_enabled() is True, f"value={v!r}"


def test_pl_explicit_false_via_env() -> None:
    for v in ("false", "0", "no", "off"):
        with mock.patch.dict(os.environ, {"BOWHEAD_HOT_WARM_COLD_ENABLED": v}):
            assert is_state_lifecycle_enabled() is False, f"value={v!r}"


def test_pl_empty_string_default_on() -> None:
    """Empty string is treated as unset → default ON."""
    with mock.patch.dict(os.environ, {"BOWHEAD_HOT_WARM_COLD_ENABLED": ""}):
        assert is_state_lifecycle_enabled() is True
