"""Tests for bowhead_core.metrics (#107)."""

from __future__ import annotations

import pytest
from bowhead_core.metrics import (
    LABEL_COMBO_LIMIT,
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    get_registry,
    inc,
    observe,
    set_gauge,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个 test 前清 registry —— 避免 counter 跨测累加."""
    get_registry().reset()
    yield
    get_registry().reset()


def test_counter_basic_inc() -> None:
    c = Counter(name="foo")
    c.inc()
    c.inc(value=2.5)
    assert c._values[()] == 3.5


def test_counter_with_labels() -> None:
    c = Counter(name="foo")
    c.inc(labels={"status": "ok"})
    c.inc(labels={"status": "ok"})
    c.inc(labels={"status": "err"})
    assert c._values[(("status", "ok"),)] == 2.0
    assert c._values[(("status", "err"),)] == 1.0


def test_counter_label_cap() -> None:
    """超过 LABEL_COMBO_LIMIT 后 drop + warn，不崩."""
    c = Counter(name="foo")
    for i in range(LABEL_COMBO_LIMIT + 20):
        c.inc(labels={"i": str(i)})
    assert len(c._values) == LABEL_COMBO_LIMIT  # Excess combinations are dropped.


def test_histogram_basic() -> None:
    h = Histogram(name="lat", buckets=(0.1, 1.0, 10.0))
    h.observe(0.05)
    h.observe(0.5)
    h.observe(5.0)
    h.observe(50.0)
    _, count, total = h._state[()]
    assert count == 4
    assert total == pytest.approx(55.55)


def test_gauge_replaces_current_value() -> None:
    gauge = Gauge(name="pending")
    gauge.set(3)
    gauge.set(1)
    assert gauge._values[()] == 1.0


def test_histogram_bucket_distribution() -> None:
    h = Histogram(name="lat", buckets=(1.0, 2.0, 5.0))
    h.observe(0.5)  # <=1, <=2, <=5
    h.observe(1.5)  # <=2, <=5
    h.observe(3.0)  # <=5
    h.observe(10.0)  # none
    buckets, _, _ = h._state[()]
    # observe() stores cumulative counts (each sample is added to all buckets where it is ≤le).
    assert buckets[0] == 1  # 0.5
    assert buckets[1] == 2  # 0.5 + 1.5
    assert buckets[2] == 3  # 0.5 + 1.5 + 3.0


def test_registry_singleton() -> None:
    r1 = get_registry()
    r2 = get_registry()
    assert r1 is r2


def test_registry_get_counter_reuses() -> None:
    r = MetricsRegistry()
    c1 = r.counter("foo")
    c2 = r.counter("foo")
    assert c1 is c2


def test_render_prometheus_counter() -> None:
    r = MetricsRegistry()
    c = r.counter("bowhead_turns_total", help="Total turns.")
    c.inc(labels={"status": "ok", "intent": "report"})
    c.inc(labels={"status": "ok", "intent": "report"})
    c.inc(labels={"status": "err", "intent": "lookup"})
    out = r.render_prometheus()
    assert "# HELP bowhead_turns_total Total turns." in out
    assert "# TYPE bowhead_turns_total counter" in out
    assert 'bowhead_turns_total{intent="report",status="ok"} 2.0' in out
    assert 'bowhead_turns_total{intent="lookup",status="err"} 1.0' in out


def test_render_prometheus_histogram() -> None:
    r = MetricsRegistry()
    h = r.histogram("lat", buckets=(1.0, 10.0))
    h.observe(0.5)
    h.observe(5.0)
    out = r.render_prometheus()
    assert "# TYPE lat histogram" in out
    assert 'lat_bucket{le="1.0"} 1' in out
    assert 'lat_bucket{le="10.0"} 2' in out
    assert 'lat_bucket{le="+Inf"} 2' in out
    assert "lat_count 2" in out
    assert "lat_sum 5.5" in out


def test_render_prometheus_gauge() -> None:
    registry = MetricsRegistry()
    gauge = registry.gauge("bowhead_pending", help="Pending work.")
    gauge.set(7)
    out = registry.render_prometheus()
    assert "# HELP bowhead_pending Pending work." in out
    assert "# TYPE bowhead_pending gauge" in out
    assert "bowhead_pending 7.0" in out


def test_module_level_helpers() -> None:
    inc("bowhead_test_c", labels={"k": "v"}, help="test")
    observe("bowhead_test_h", 0.42, labels={"k": "v"}, help="test")
    set_gauge("bowhead_test_g", 4, labels={"k": "v"}, help="test")
    out = get_registry().render_prometheus()
    assert "bowhead_test_c" in out
    assert "bowhead_test_h_count" in out
    assert 'bowhead_test_g{k="v"} 4.0' in out


def test_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOWHEAD_METRICS_ENABLED=0 → inc/observe no-op."""
    monkeypatch.setenv("BOWHEAD_METRICS_ENABLED", "0")
    c = Counter(name="disabled")
    c.inc()
    c.inc()
    assert c._values == {}


def test_labels_escape_quote_backslash() -> None:
    r = MetricsRegistry()
    c = r.counter("foo")
    c.inc(labels={"tool": 'name"with\\quote'})
    out = r.render_prometheus()
    assert 'name\\"with\\\\quote' in out
