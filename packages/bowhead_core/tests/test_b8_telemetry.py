"""bi-hitl-cleanup P8 / B8 telemetry tests.

Per tech-design §P8:
- L0: `bowhead_clarification_ask_call_total` w/ {type, caller, source_kind, outcome}
  - emit from clarification_tool handler (both B5 questions + legacy candidates shapes)
- L1 BI: `bowhead_bi_concept_resolve_call_total` (P1 — already done)
- L1 BI: `bowhead_bi_query_call_total` (this phase) w/ {caller, n_queries bucket}

I1: every clarification emit increments call counter.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from bowhead_core import metrics


@pytest.fixture
def _captured_metrics():
    """Capture metrics.inc() calls during test."""
    calls: list[dict[str, Any]] = []
    orig_inc = metrics.inc

    def _wrap(name: str, *, labels: dict[str, str] | None = None, **kw: Any) -> None:
        calls.append({"name": name, "labels": labels or {}, **kw})

    metrics.inc = _wrap  # type: ignore[assignment]
    try:
        yield calls
    finally:
        metrics.inc = orig_inc  # type: ignore[assignment]


# ── clarification_tool: B5 questions shape (already done P5) ──────────


@pytest.mark.asyncio
async def test_b8_clarification_ask_questions_shape_emits_counter(_captured_metrics) -> None:
    """B5 schema (questions field) emits counter w/ proper labels."""
    from bowhead_core.agents.clarification_tool import _clarification_ask

    ctx = SimpleNamespace(turn_id="t_b8_1")
    await _clarification_ask(
        {
            "questions": [
                {
                    "question": "客单价用哪个公式?",
                    "options": ["GMV/订单数", "净销售额/订单数"],
                    "type": "single_select",
                }
            ],
        },
        ctx,
    )

    relevant = [c for c in _captured_metrics if c["name"] == "bowhead_clarification_ask_call_total"]
    assert len(relevant) == 1
    labels = relevant[0]["labels"]
    assert labels["type"] == "single_select"
    assert labels["caller"] == "tool_handler"
    assert labels["source_kind"] == "generic"
    assert labels["outcome"] == "emitted"


# ── clarification_tool: legacy candidates shape (P8 gap fix) ──────────


@pytest.mark.asyncio
async def test_b8_clarification_ask_legacy_candidates_emits_counter(_captured_metrics) -> None:
    """P8 gap fix: legacy candidates shape now also emits counter (was 0 before)."""
    from bowhead_core.agents.clarification_tool import _clarification_ask

    ctx = SimpleNamespace(turn_id="t_b8_2")
    await _clarification_ask(
        {
            "candidates": ["GMV同比", "净销售同比"],
            "fallback_text": "请选择想看的指标",
        },
        ctx,
    )

    relevant = [c for c in _captured_metrics if c["name"] == "bowhead_clarification_ask_call_total"]
    assert len(relevant) == 1
    labels = relevant[0]["labels"]
    assert labels["caller"] == "tool_handler_legacy", (
        "legacy candidates path should emit w/ caller=tool_handler_legacy so we can distinguish B5 migration progress"
    )
    assert labels["source_kind"] == "generic"
    assert labels["outcome"] == "emitted"


# ── bi_query top-level counter (P8 new) ───────────────────────────────


@pytest.mark.asyncio
async def test_b8_bi_query_top_level_emits_counter(_captured_metrics) -> None:
    """bi_query handler emits `bowhead_bi_query_call_total` per top-level call."""
    from bowhead_core.tool_registry import build_default_registry

    reg = build_default_registry()
    spec = reg._specs["bi_query"]

    ctx = SimpleNamespace(
        turn_id="t_b8_3",
        workspace_id="ws_test",
        member_uid="u_test",
        _running_profile="compute",
    )
    # Use empty queries path (returns no_queries early — doesn't need datarc)
    # But wait, empty path returns BEFORE telemetry. So pass 1 query, but
    # datarc will fail. We need to set up a mock or accept that the call
    # raises after telemetry emits.
    with contextlib.suppress(Exception):
        await spec.handler({"queries": []}, ctx)
    # empty path returns before telemetry emit, so verify with non-empty
    # path instead. Use bypass approach: catch the exception, check counter
    # emitted before failure.

    # Reset captured
    _captured_metrics.clear()
    try:  # noqa: SIM105
        await spec.handler({"queries": ["test query"]}, ctx)
    except Exception:  # noqa: S110
        pass  # datarc not available in UT — call fails after telemetry

    relevant = [c for c in _captured_metrics if c["name"] == "bowhead_bi_query_call_total"]
    assert len(relevant) >= 1, f"bi_query should emit call counter; got metrics: {[c['name'] for c in _captured_metrics]}"
    labels = relevant[0]["labels"]
    assert labels["caller"] == "compute"
    assert labels["n_queries"] == "1"


@pytest.mark.asyncio
async def test_b8_bi_query_n_queries_bucket_2_5(_captured_metrics) -> None:
    """n_queries bucket label: 2-5 for batch of 3."""
    from bowhead_core.tool_registry import build_default_registry

    reg = build_default_registry()
    spec = reg._specs["bi_query"]

    ctx = SimpleNamespace(turn_id="t_b8_4", _running_profile="compute")
    with contextlib.suppress(Exception):
        await spec.handler({"queries": ["a", "b", "c"]}, ctx)

    relevant = [c for c in _captured_metrics if c["name"] == "bowhead_bi_query_call_total"]
    assert len(relevant) >= 1
    assert relevant[0]["labels"]["n_queries"] == "2-5"


@pytest.mark.asyncio
async def test_b8_bi_query_caller_fallback_to_lead(_captured_metrics) -> None:
    """If ctx has no _running_profile attr (Lead-direct call), defaults to 'lead'.

    P9 SRE fix: matches bi_concept_resolve.py:403 fallback semantics —
    `_running_profile` is set by SubagentRunner only; absence means Lead is
    invoking directly.
    """
    from bowhead_core.tool_registry import build_default_registry

    reg = build_default_registry()
    spec = reg._specs["bi_query"]

    ctx = SimpleNamespace(turn_id="t_b8_5")  # no _running_profile
    with contextlib.suppress(Exception):
        await spec.handler({"queries": ["x"]}, ctx)

    relevant = [c for c in _captured_metrics if c["name"] == "bowhead_bi_query_call_total"]
    assert len(relevant) >= 1
    assert relevant[0]["labels"]["caller"] == "lead"
