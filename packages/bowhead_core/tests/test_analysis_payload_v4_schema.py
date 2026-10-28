"""chat-v4 CHANGE-S.* schema migration tests.

Covers:
- CHANGE-S.1: Kpi sentiment "warning" round-trips
- CHANGE-S.7: Insight module_num + signal optional fields
- W5 (merged into W4): multi_source_compare subfield + length caps
- W5: findings optional list + cap
"""

from __future__ import annotations

from dataclasses import asdict

import pytest
from bowhead_core.analysis_payload import (
    AnalysisPayload,
    EvidenceBlock,
    Insight,
    Kpi,
    Recommendation,
    payload_from_dict,
    validate_payload,
)


def _good_dict() -> dict:
    return {
        "title": "T",
        "exec_summary": "executive summary text long enough to satisfy validators OK ok ok",
        "kpis": [{"label": "L", "value": "1%", "sentiment": "neutral"}],
        "insights": [
            {
                "title": "i1",
                "thesis": "thesis text 51.6%",
                "evidence": [
                    {"kind": "chart", "chart_section_id": "sec_a", "caption": "c"},
                ],
                "narrative": "x" * 50,
                "confidence": "high",
            }
        ],
        "recommendations": [{"priority": "P1", "action": "do x"}],
    }


# ─── CHANGE-S.1: Kpi sentiment "warning" ────────────────────────────────


@pytest.mark.unit
def test_kpi_warning_validate_ok() -> None:
    d = _good_dict()
    d["kpis"][0]["sentiment"] = "warning"
    ok, errs = validate_payload(d)
    assert ok, errs


@pytest.mark.unit
def test_kpi_warning_roundtrip() -> None:
    """CHANGE-S.1 + S.3: warning survives validate → from_dict → asdict cycle."""
    d = _good_dict()
    d["kpis"][0]["sentiment"] = "warning"
    ok, _ = validate_payload(d)
    assert ok
    p = payload_from_dict(d)
    assert p.kpis[0].sentiment == "warning"
    rt = asdict(p)
    assert rt["kpis"][0]["sentiment"] == "warning"


@pytest.mark.unit
def test_kpi_invalid_sentiment_rejected() -> None:
    d = _good_dict()
    d["kpis"][0]["sentiment"] = "danger"  # not in {good,bad,neutral,warning}
    ok, errs = validate_payload(d)
    assert not ok
    assert any("sentiment" in e for e in errs)


# ─── CHANGE-S.7: Insight module_num + signal ────────────────────────────


@pytest.mark.unit
def test_insight_module_num_signal_round_trip() -> None:
    d = _good_dict()
    d["insights"][0]["module_num"] = "M3"
    d["insights"][0]["signal"] = "warn"
    ok, errs = validate_payload(d)
    assert ok, errs
    p = payload_from_dict(d)
    assert p.insights[0].module_num == "M3"
    assert p.insights[0].signal == "warn"


@pytest.mark.unit
def test_insight_bad_module_num_rejected() -> None:
    d = _good_dict()
    d["insights"][0]["module_num"] = "M99"  # not in M1..M7
    ok, errs = validate_payload(d)
    assert not ok
    assert any("module_num" in e for e in errs)


@pytest.mark.unit
def test_insight_bad_signal_rejected() -> None:
    d = _good_dict()
    d["insights"][0]["signal"] = "fatal"
    ok, errs = validate_payload(d)
    assert not ok
    assert any("signal" in e for e in errs)


# ─── W5: multi_source_compare + caps ─────────────────────────────────────


@pytest.mark.unit
def test_multi_source_compare_round_trip() -> None:
    d = _good_dict()
    d["multi_source_compare"] = {
        "sources": [{"name": "BI", "kind": "internal"}],
        "rows": [{"label": "GMV", "values": {"BI": 100.0}}],
        "verdict": {
            "rows": [{"label": "GMV", "verdict": "agree", "confidence": "high"}],
        },
    }
    ok, errs = validate_payload(d)
    assert ok, errs
    p = payload_from_dict(d)
    assert p.multi_source_compare is not None
    assert p.multi_source_compare["rows"][0]["label"] == "GMV"


@pytest.mark.unit
def test_multi_source_compare_rows_cap() -> None:
    d = _good_dict()
    d["multi_source_compare"] = {
        "rows": [{"label": f"r{i}"} for i in range(13)],  # 13 > 12 cap
    }
    ok, errs = validate_payload(d)
    assert not ok
    assert any("multi_source_compare.rows" in e for e in errs)


@pytest.mark.unit
def test_multi_source_compare_quote_excerpt_cap() -> None:
    d = _good_dict()
    d["multi_source_compare"] = {
        "rows": [{"label": "r0", "quote_excerpt": "x" * 281}],
    }
    ok, errs = validate_payload(d)
    assert not ok
    assert any("quote_excerpt" in e for e in errs)


@pytest.mark.unit
def test_findings_cap() -> None:
    d = _good_dict()
    d["findings"] = [{"text": f"f{i}"} for i in range(21)]  # > 20
    ok, errs = validate_payload(d)
    assert not ok
    assert any("findings" in e for e in errs)


@pytest.mark.unit
def test_findings_under_cap_ok() -> None:
    d = _good_dict()
    d["findings"] = [{"text": f"f{i}"} for i in range(20)]
    ok, errs = validate_payload(d)
    assert ok, errs
    p = payload_from_dict(d)
    assert p.findings is not None
    assert len(p.findings) == 20


@pytest.mark.unit
def test_multi_source_verdict_invalid_confidence() -> None:
    d = _good_dict()
    d["multi_source_compare"] = {
        "verdict": {
            "rows": [{"label": "x", "confidence": "VERY_HIGH"}],
        },
    }
    ok, errs = validate_payload(d)
    assert not ok
    assert any("verdict" in e and "confidence" in e for e in errs)


# ─── CHANGE-S.6: actions ↔ recommendations alias (asdict / dual-read) ────


# ─── chat-v4 W4-fix CTO P0-4 — partial_failure + RetryAction round-trip ──


@pytest.mark.unit
def test_partial_failure_roundtrip() -> None:
    """P0-4: partial_failure with retry_action round-trips through
    payload_from_dict + asdict, and asdict surfaces subtask_id for FE.
    """
    from bowhead_core.analysis_payload import (
        payload_from_dict,
    )

    d = _good_dict()
    d["partial_failure"] = {
        "what_missed": "Q2 2024 revenue",
        "what_used_instead": "Q1 2024 + estimate",
        "impact": "trend confidence reduced to med",
        "retry_action": {
            "kind": "retry_subtask",
            "cta_label": "重试 Q2 拉取",
            "subtask_id": "sub_q2_revenue",
        },
    }
    # Round-trip via payload_from_dict (validate_payload is independent of
    # partial_failure since it's optional metadata).
    p = payload_from_dict(d)
    assert p.partial_failure is not None
    assert p.partial_failure.what_missed == "Q2 2024 revenue"
    assert p.partial_failure.retry_action is not None
    assert p.partial_failure.retry_action.subtask_id == "sub_q2_revenue"
    assert p.partial_failure.retry_action.kind == "retry_subtask"

    # asdict surfaces subtask_id for FE consumption.
    rt = asdict(p)
    assert rt["partial_failure"]["retry_action"]["subtask_id"] == "sub_q2_revenue"
    assert rt["partial_failure"]["retry_action"]["kind"] == "retry_subtask"


@pytest.mark.unit
def test_partial_failure_optional_retry_action() -> None:
    """retry_action is optional — partial_failure can stand alone."""
    from bowhead_core.analysis_payload import payload_from_dict

    d = _good_dict()
    d["partial_failure"] = {
        "what_missed": "x",
        "what_used_instead": "y",
        "impact": "z",
    }
    p = payload_from_dict(d)
    assert p.partial_failure is not None
    assert p.partial_failure.retry_action is None


@pytest.mark.unit
def test_no_partial_failure_default_none() -> None:
    """AnalysisPayload.partial_failure defaults to None when absent."""
    from bowhead_core.analysis_payload import payload_from_dict

    p = payload_from_dict(_good_dict())
    assert p.partial_failure is None


@pytest.mark.unit
def test_recommendations_to_dict_via_asdict() -> None:
    """CTO P0: use stdlib asdict() for dataclass→dict (NOT a custom to_dict()
    method on AnalysisPayload). emit-site aliases `actions` at dict level."""
    p = AnalysisPayload(
        title="T",
        exec_summary="x" * 31,
        kpis=[Kpi(label="L", value="1")],
        insights=[
            Insight(
                title="i",
                thesis="t",
                evidence=[EvidenceBlock(kind="chart", caption="c", chart_section_id="s")],
                narrative="x" * 50,
            )
        ],
        recommendations=[Recommendation(priority="P1", action="do")],
    )
    d = asdict(p)
    assert d["recommendations"][0]["action"] == "do"
    # Verify there is NO custom to_dict method on the dataclass.
    assert not hasattr(p, "to_dict") or asdict.__module__ == "dataclasses"
