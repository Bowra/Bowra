"""Sprint ι Phase C (PRD §4.C.2 / tech-r2 §2.7).

Tests for the auto-chart fallback:
- ``should_auto_chart`` 5-condition AND-gate (boundary cases)
- ``collect_auto_chart_candidates`` two-pass walk + cap
- ``peer_supports_auto_chart`` I-ι1 / I-ι3 invariant whitelist
- Feature flag (BOWHEAD_IOTA_AUTO_CHART_FALLBACK=0 disables module)
- ``_looks_numeric_col`` detector
"""

from __future__ import annotations

import os

import pytest
from bowhead_core.canvas_composers.auto_chart import (
    DEFAULT_PER_TURN_CAP,
    _looks_numeric_col,
    collect_auto_chart_candidates,
    peer_supports_auto_chart,
    should_auto_chart,
)
from bowhead_core.types import ToolInvocation

# ───────────────────────────────────────────────────────────────
# peer_supports_auto_chart (invariant I-ι1 / I-ι3)
# ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("compute", True),
        ("financial-analyst", True),
        ("business-analyst", True),
        ("kb-analyst", False),
        ("canvas-writer", False),
        ("lead", False),
        ("", False),
    ],
)
def test_peer_supports_auto_chart_whitelist(profile: str, expected: bool) -> None:
    assert peer_supports_auto_chart(profile) is expected


# ───────────────────────────────────────────────────────────────
# should_auto_chart 5-condition AND-gate
# ───────────────────────────────────────────────────────────────


def _meta(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "artifact_id": "art_x",
        "mime": "text/csv",
        "numeric_col_count": 1,
        "row_count": 5,
    }
    base.update(kwargs)
    return base


def test_passes_5_and_gate_canonical() -> None:
    assert should_auto_chart(
        _meta(),
        consumed_artifact_ids=set(),
        chart_referenced_artifact_ids=set(),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("mime", "image/png"),  # cond 1 fail
        ("mime", "application/json"),
        ("numeric_col_count", 0),  # cond 2 fail (too few)
        ("numeric_col_count", 3),  # cond 2 fail (too many)
        ("row_count", 2),  # cond 3 fail (too few)
        ("row_count", 201),  # cond 3 fail (too many)
        ("artifact_id", None),  # missing aid
        ("artifact_id", ""),
    ],
)
def test_each_gate_condition_blocks(field: str, value: object) -> None:
    meta = _meta(**{field: value})
    assert not should_auto_chart(
        meta,
        consumed_artifact_ids=set(),
        chart_referenced_artifact_ids=set(),
    )


def test_cond4_consumed_artifact_blocked() -> None:
    assert not should_auto_chart(
        _meta(artifact_id="art_consumed"),
        consumed_artifact_ids={"art_consumed"},
        chart_referenced_artifact_ids=set(),
    )


def test_cond5_already_charted_blocked() -> None:
    assert not should_auto_chart(
        _meta(artifact_id="art_referenced"),
        consumed_artifact_ids=set(),
        chart_referenced_artifact_ids={"art_referenced"},
    )


def test_boundary_lower_bounds_pass() -> None:
    """row_count=3 + numeric_col_count=1 (lower edges) should pass."""
    assert should_auto_chart(
        _meta(row_count=3, numeric_col_count=1),
        consumed_artifact_ids=set(),
        chart_referenced_artifact_ids=set(),
    )


def test_boundary_upper_bounds_pass() -> None:
    """row_count=200 + numeric_col_count=2 (upper edges) should pass."""
    assert should_auto_chart(
        _meta(row_count=200, numeric_col_count=2),
        consumed_artifact_ids=set(),
        chart_referenced_artifact_ids=set(),
    )


# ───────────────────────────────────────────────────────────────
# Feature flag
# ───────────────────────────────────────────────────────────────


def test_flag_off_blocks_all_charts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_IOTA_AUTO_CHART_FALLBACK", "0")
    assert not should_auto_chart(
        _meta(),
        consumed_artifact_ids=set(),
        chart_referenced_artifact_ids=set(),
    )
    assert collect_auto_chart_candidates([]) == []


def test_flag_on_default_allows() -> None:
    if "BOWHEAD_IOTA_AUTO_CHART_FALLBACK" in os.environ:
        del os.environ["BOWHEAD_IOTA_AUTO_CHART_FALLBACK"]
    assert should_auto_chart(
        _meta(),
        consumed_artifact_ids=set(),
        chart_referenced_artifact_ids=set(),
    )


# ───────────────────────────────────────────────────────────────
# _looks_numeric_col detector
# ───────────────────────────────────────────────────────────────


def test_looks_numeric_col_pure_numeric() -> None:
    rows = [["1.0", "a"], ["2.0", "b"], ["3", "c"]]
    assert _looks_numeric_col(rows, 0)
    assert not _looks_numeric_col(rows, 1)


def test_looks_numeric_col_threshold_80_percent() -> None:
    # 4/5 numeric (80%) → pass; 3/5 (60%) → fail.
    pass_rows = [["1"], ["2"], ["3"], ["4"], ["x"]]
    fail_rows = [["1"], ["2"], ["3"], ["x"], ["y"]]
    assert _looks_numeric_col(pass_rows, 0)
    assert not _looks_numeric_col(fail_rows, 0)


def test_looks_numeric_col_thousand_separator() -> None:
    rows = [["1,234"], ["5,678"], ["9,000"]]
    assert _looks_numeric_col(rows, 0)


def test_looks_numeric_col_empty_or_short() -> None:
    assert not _looks_numeric_col([], 0)
    assert not _looks_numeric_col([[""], [""]], 0)


# ───────────────────────────────────────────────────────────────
# collect_auto_chart_candidates two-pass walk
# ───────────────────────────────────────────────────────────────


def _make_inv(
    tool_name: str,
    *,
    arguments: dict | None = None,
    result: dict | None = None,
    error: str | None = None,
) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_name,
        arguments=arguments or {},
        result=result,
        error=error,
        started_at="2026-05-09T00:00:00Z",
        ended_at="2026-05-09T00:00:01Z",
    )


def _sandbox_inv_with_csv(
    artifact_id: str,
    *,
    rows: int = 5,
    n_numeric_cols: int = 1,
) -> ToolInvocation:
    cols = [f"c{i}" for i in range(n_numeric_cols)] + ["label"]
    sample_rows = [[str(i * 10) for _ in range(n_numeric_cols)] + [f"row_{i}"] for i in range(rows)]
    return _make_inv(
        "python_sandbox",
        result={
            "outputs": [
                {
                    "artifact_id": artifact_id,
                    "mime": "text/csv",
                    "name": f"{artifact_id}.csv",
                    "preview": {"columns": cols, "rows": sample_rows},
                }
            ],
        },
    )


def test_collect_emits_for_unconsumed_uncharted_csv() -> None:
    invs = [_sandbox_inv_with_csv("art_y")]
    candidates = collect_auto_chart_candidates(invs)
    assert len(candidates) == 1
    assert candidates[0]["data_artifact_id"] == "art_y"
    assert candidates[0]["auto_charted"] is True
    assert candidates[0]["hint"] == "auto"


def test_collect_skips_consumed_artifact() -> None:
    """If a downstream sandbox call inputs this artifact, skip auto-chart."""
    art = "art_consumed"
    sandbox_emit = _sandbox_inv_with_csv(art)
    downstream_consume = _make_inv(
        "python_sandbox",
        arguments={"inputs": {"x": {"artifact_id": art}}},
        result={"outputs": []},
    )
    invs = [sandbox_emit, downstream_consume]
    assert collect_auto_chart_candidates(invs) == []


def test_collect_skips_already_charted() -> None:
    art = "art_already_charted"
    sandbox_emit = _sandbox_inv_with_csv(art)
    explicit_chart = _make_inv(
        "add_chart",
        arguments={"data_artifact_id": art},
    )
    invs = [sandbox_emit, explicit_chart]
    assert collect_auto_chart_candidates(invs) == []


def test_collect_caps_at_default() -> None:
    invs = [_sandbox_inv_with_csv(f"art_{i}") for i in range(5)]
    candidates = collect_auto_chart_candidates(invs)
    assert len(candidates) == DEFAULT_PER_TURN_CAP


def test_collect_caps_at_explicit_value() -> None:
    invs = [_sandbox_inv_with_csv(f"art_{i}") for i in range(5)]
    candidates = collect_auto_chart_candidates(invs, cap=1)
    assert len(candidates) == 1


def test_collect_skips_failed_invocations() -> None:
    sandbox_fail = _make_inv("python_sandbox", error="oom")
    sandbox_ok = _sandbox_inv_with_csv("art_ok")
    invs = [sandbox_fail, sandbox_ok]
    candidates = collect_auto_chart_candidates(invs)
    assert len(candidates) == 1
    assert candidates[0]["data_artifact_id"] == "art_ok"


def test_collect_skips_non_sandbox_tools() -> None:
    bi_inv = _make_inv(
        "bi_ask_question",
        result={
            "outputs": [
                {
                    "artifact_id": "art_bi",
                    "mime": "text/csv",
                    "name": "bi.csv",
                    "preview": {"columns": ["c1"], "rows": [["1"], ["2"], ["3"], ["4"]]},
                }
            ]
        },
    )
    assert collect_auto_chart_candidates([bi_inv]) == []


def test_collect_handles_malformed_outputs_gracefully() -> None:
    """Defensive: non-list outputs / missing preview / non-dict rows etc.
    don't raise — caller's exception handler is the safety net but module
    itself shouldn't rely on it."""
    invs = [
        _make_inv("python_sandbox", result={"outputs": "not_a_list"}),
        _make_inv("python_sandbox", result={"outputs": [{}]}),
        _make_inv("python_sandbox", result={"outputs": [{"preview": "not_dict"}]}),
        _make_inv("python_sandbox", result=None),
    ]
    assert collect_auto_chart_candidates(invs) == []


# ───────────────────────────────────────────────────────────────
# ι-14 P1 carry: same-turn duplicate artifact dedup (regression guard)
# ───────────────────────────────────────────────────────────────


def test_collect_dedups_same_artifact_id_within_turn() -> None:
    """If same artifact_id appears as output across two sandbox calls
    in the same turn, only the first triggers auto-chart (intra-batch
    dedup via in-loop ``referenced`` set update)."""
    sandbox_first = _sandbox_inv_with_csv("art_dup")
    # Second sandbox call emits an identical artifact_id (rare but possible
    # if runner re-uploads same DataFrame). Should NOT double-emit.
    sandbox_second = _sandbox_inv_with_csv("art_dup")
    candidates = collect_auto_chart_candidates([sandbox_first, sandbox_second])
    assert len(candidates) == 1
    assert candidates[0]["data_artifact_id"] == "art_dup"


# ───────────────────────────────────────────────────────────────
# ι-14 P0 #2: round-trip flag preservation (auto_charted reaches frontend)
# ───────────────────────────────────────────────────────────────


def test_round_trip_auto_charted_flag_through_spec_to_dict() -> None:
    """End-to-end contract: auto_chart payload → ChartSpec → spec_to_dict
    → dict carries auto_charted=True. Without this, the entire frontend
    Phase C subcaption is dead code."""
    from bowhead_core.chart.types import ChartSpec, spec_to_dict

    spec = ChartSpec(
        kind="bar",
        title="auto-chart from sandbox",
        row_count=5,
        auto_charted=True,
    )
    serialized = spec_to_dict(spec)
    assert serialized.get("auto_charted") is True


def test_round_trip_default_false_dropped_or_preserved() -> None:
    """Non-auto chart (default False) — verify whatever the policy is, it's
    consistent with frontend expectations. _drop_empty removes None/[]/{}
    but preserves False so spec.auto_charted=False arrives as `False`."""
    from bowhead_core.chart.types import ChartSpec, spec_to_dict

    spec = ChartSpec(kind="bar", title="manual chart", row_count=5)
    serialized = spec_to_dict(spec)
    # False is preserved (not dropped); frontend's `if (!auto_charted)` handles both.
    assert serialized.get("auto_charted") is False
