"""Sprint multi-agent-v1 PR-#13 — agent metrics + alerting rules 单测."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from bowhead_core.agents.metrics import (
    ALERTING_RULES,
    AlertingRule,
    emit_canvas_writer_fabricate,
    emit_dispatch,
    emit_h2_approval_outcome,
    emit_intent_classified,
    emit_manifest_load_fail,
    emit_manifest_upload,
    emit_parallel_latency,
    emit_partial_success,
    emit_redis_health,
    emit_task_failed,
    to_prometheus_yaml,
)
from bowhead_core.metrics import get_registry


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """每 test 起始 reset registry (避免测之间互相干扰)."""
    reg = get_registry()
    reg._counters.clear()
    reg._histograms.clear()


def test_emit_dispatch() -> None:
    emit_dispatch(complexity="med", agent_count=3)
    out = get_registry().render_prometheus()
    assert "bowhead_multi_agent_dispatch_total" in out
    assert 'complexity="med"' in out
    assert 'agent_count="3"' in out


def test_emit_dispatch_caps_agent_count() -> None:
    """agent_count cap 10 防 cardinality 爆."""
    emit_dispatch(complexity="high", agent_count=20)
    out = get_registry().render_prometheus()
    assert 'agent_count="10"' in out


def test_emit_parallel_latency() -> None:
    emit_parallel_latency(complexity="high", agent_count=4, latency_ms=12345.6)
    out = get_registry().render_prometheus()
    assert "bowhead_multi_agent_parallel_latency_ms" in out


def test_emit_task_failed() -> None:
    emit_task_failed(agent_name="financial-analyst", reason="Timeout")
    out = get_registry().render_prometheus()
    assert "bowhead_multi_agent_task_failed_total" in out
    assert 'agent="financial-analyst"' in out
    assert 'reason="Timeout"' in out


def test_emit_task_failed_caps_long_strings() -> None:
    emit_task_failed(agent_name="x" * 100, reason="y" * 100)
    out = get_registry().render_prometheus()
    assert "bowhead_multi_agent_task_failed_total" in out


def test_emit_partial_success() -> None:
    emit_partial_success(n_failed=1, n_total=3)
    out = get_registry().render_prometheus()
    assert "bowhead_multi_agent_partial_success_total" in out
    assert 'ratio="1/3"' in out


def test_emit_partial_success_zero_total_no_op() -> None:
    """n_total=0 不 emit (avoid div by zero)."""
    emit_partial_success(n_failed=0, n_total=0)
    out = get_registry().render_prometheus()
    # This metric must be absent.
    assert "bowhead_multi_agent_partial_success_total" not in out


def test_emit_canvas_writer_fabricate() -> None:
    emit_canvas_writer_fabricate()
    out = get_registry().render_prometheus()
    assert "bowhead_canvas_writer_fabricate_detected_total" in out


def test_emit_intent_classified() -> None:
    emit_intent_classified(intent="work")
    out = get_registry().render_prometheus()
    assert "bowhead_intent_classified_total_v2" in out
    assert 'intent="work"' in out


def test_emit_redis_health() -> None:
    emit_redis_health(2)
    # health is histogram (gauge-like), check it was registered
    reg = get_registry()
    assert "redis_health_status" in reg._histograms


def test_emit_h2_approval_outcome() -> None:
    emit_h2_approval_outcome(outcome="confirm")
    out = get_registry().render_prometheus()
    assert "bowhead_h2_approval_outcome_total" in out
    assert 'outcome="confirm"' in out


def test_emit_manifest_load_fail() -> None:
    emit_manifest_load_fail(source="system", reason="path_traversal")
    out = get_registry().render_prometheus()
    assert "bowhead_manifest_load_fail_total" in out
    assert 'source="system"' in out


def test_emit_manifest_upload() -> None:
    emit_manifest_upload(status="rejected")
    out = get_registry().render_prometheus()
    assert "bowhead_manifest_upload_total" in out
    assert 'status="rejected"' in out


# -- Alerting rules --


def test_alerting_rules_count() -> None:
    """8 alerting rules total per §9.4."""
    assert len(ALERTING_RULES) == 8


def test_alerting_rules_have_p0_for_fabricate() -> None:
    """canvas-writer fabricate 是 P0 (immediate page)."""
    fabricate_rule = next((r for r in ALERTING_RULES if "Fabricate" in r.name), None)
    assert fabricate_rule is not None
    assert fabricate_rule.page_level == "P0"


def test_alerting_rules_have_p1_for_redis_down() -> None:
    redis_rule = next((r for r in ALERTING_RULES if "Redis" in r.name), None)
    assert redis_rule is not None
    assert redis_rule.page_level == "P1"


def test_alerting_rule_dataclass_frozen() -> None:
    """AlertingRule 是 frozen dataclass."""
    r = AlertingRule(
        name="x",
        metric="m",
        threshold="m > 0",
        window="5m",
        page_level="P2",
        description="x",
    )
    with pytest.raises((AttributeError, FrozenInstanceError)):
        r.name = "y"  # type: ignore


def test_to_prometheus_yaml() -> None:
    """生成 Prometheus rules.yaml format."""
    yaml_str = to_prometheus_yaml()
    assert "groups:" in yaml_str
    assert "name: bowhead_multi_agent_alerts" in yaml_str
    assert "alert:" in yaml_str
    assert "severity:" in yaml_str
    # Every rule has an alert line.
    alert_lines = [line for line in yaml_str.split("\n") if "alert:" in line]
    assert len(alert_lines) == len(ALERTING_RULES)


def test_alerting_rules_distinct_names() -> None:
    """每 rule name 唯一."""
    names = [r.name for r in ALERTING_RULES]
    assert len(set(names)) == len(names)


def test_alerting_rules_all_page_levels_valid() -> None:
    valid_levels = {"P0", "P1", "P2", "P3"}
    for r in ALERTING_RULES:
        assert r.page_level in valid_levels
