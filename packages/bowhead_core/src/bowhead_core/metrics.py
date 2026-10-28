"""In-process metrics registry (#107).

Zero-dep counter / histogram collection. Renders Prometheus text format v0.0.4
so a future scraper or the gateway `/metrics` endpoint can expose it.

设计要点（见 docs/metrics-outlet-design.md）:
- 纯 python dict + threading.Lock，gateway / orchestrator 共用单例 registry
- labels 无界风险：每个 metric 超过 LABEL_COMBO_LIMIT 个 combination 时 drop
- 全局 `BOWHEAD_METRICS_ENABLED=0` 环境变量可整体关掉（no-op 路径）
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

LABEL_COMBO_LIMIT = 128
DEFAULT_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)


def _enabled() -> bool:
    return (os.environ.get("BOWHEAD_METRICS_ENABLED") or "1") != "0"


def _labels_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _fmt_labels(key: tuple[tuple[str, str], ...]) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
    return "{" + inner + "}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class Counter:
    name: str
    help: str = ""
    label_combo_limit: int = LABEL_COMBO_LIMIT
    _values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)

    def inc(self, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        if not _enabled():
            return
        key = _labels_key(labels)
        if key not in self._values and len(self._values) >= self.label_combo_limit:
            logger.warning("metrics.counter.label_cap_hit name=%s cap=%d", self.name, self.label_combo_limit)
            return
        self._values[key] = self._values.get(key, 0.0) + float(value)

    def render(self) -> list[str]:
        lines: list[str] = []
        if self.help:
            lines.append(f"# HELP {self.name} {self.help}")
        lines.append(f"# TYPE {self.name} counter")
        for key, v in self._values.items():
            lines.append(f"{self.name}{_fmt_labels(key)} {v}")
        return lines


@dataclass
class Gauge:
    name: str
    help: str = ""
    label_combo_limit: int = LABEL_COMBO_LIMIT
    _values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        if not _enabled():
            return
        key = _labels_key(labels)
        if key not in self._values and len(self._values) >= self.label_combo_limit:
            logger.warning(
                "metrics.gauge.label_cap_hit name=%s cap=%d",
                self.name,
                self.label_combo_limit,
            )
            return
        self._values[key] = float(value)

    def render(self) -> list[str]:
        lines: list[str] = []
        if self.help:
            lines.append(f"# HELP {self.name} {self.help}")
        lines.append(f"# TYPE {self.name} gauge")
        for key, value in self._values.items():
            lines.append(f"{self.name}{_fmt_labels(key)} {value}")
        return lines


@dataclass
class Histogram:
    name: str
    help: str = ""
    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    label_combo_limit: int = LABEL_COMBO_LIMIT
    # per label-key: (bucket_counts list, count, sum)
    _state: dict[tuple[tuple[str, str], ...], list[Any]] = field(default_factory=dict)

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        if not _enabled():
            return
        key = _labels_key(labels)
        if key not in self._state and len(self._state) >= self.label_combo_limit:
            logger.warning("metrics.histogram.label_cap_hit name=%s cap=%d", self.name, self.label_combo_limit)
            return
        st = self._state.get(key)
        if st is None:
            st = [[0] * len(self.buckets), 0, 0.0]
            self._state[key] = st
        bucket_counts, count, total = st
        for i, le in enumerate(self.buckets):
            if value <= le:
                bucket_counts[i] += 1
        st[1] = count + 1
        st[2] = total + float(value)

    def render(self) -> list[str]:
        lines: list[str] = []
        if self.help:
            lines.append(f"# HELP {self.name} {self.help}")
        lines.append(f"# TYPE {self.name} histogram")
        # observe() already adds each sample to all buckets where it is ≤le; stored counts are cumulative.
        # Emit them directly per the Prometheus convention (do not accumulate them again).
        for key, (bucket_counts, count, total) in self._state.items():
            for i, le in enumerate(self.buckets):
                bucket_labels = dict(key) | {"le": str(le)}
                bk = _labels_key(bucket_labels)
                lines.append(f"{self.name}_bucket{_fmt_labels(bk)} {bucket_counts[i]}")
            inf_labels = dict(key) | {"le": "+Inf"}
            inf_k = _labels_key(inf_labels)
            lines.append(f"{self.name}_bucket{_fmt_labels(inf_k)} {count}")
            lines.append(f"{self.name}_count{_fmt_labels(key)} {count}")
            lines.append(f"{self.name}_sum{_fmt_labels(key)} {total}")
        return lines


class MetricsRegistry:
    """Process-wide metric registry. Singleton via `get_registry()`."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()
        self.started_at = time.time()

    def counter(
        self,
        name: str,
        help: str = "",  # noqa: A002
        label_combo_limit: int = LABEL_COMBO_LIMIT,
    ) -> Counter:
        with self._lock:
            c = self._counters.get(name)
            if c is None:
                c = Counter(
                    name=name,
                    help=help,
                    label_combo_limit=label_combo_limit,
                )
                self._counters[name] = c
            return c

    def gauge(
        self,
        name: str,
        help: str = "",  # noqa: A002
        label_combo_limit: int = LABEL_COMBO_LIMIT,
    ) -> Gauge:
        with self._lock:
            gauge = self._gauges.get(name)
            if gauge is None:
                gauge = Gauge(
                    name=name,
                    help=help,
                    label_combo_limit=label_combo_limit,
                )
                self._gauges[name] = gauge
            return gauge

    def histogram(
        self,
        name: str,
        help: str = "",  # noqa: A002
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
        label_combo_limit: int = LABEL_COMBO_LIMIT,
    ) -> Histogram:
        with self._lock:
            h = self._histograms.get(name)
            if h is None:
                h = Histogram(
                    name=name,
                    help=help,
                    buckets=buckets,
                    label_combo_limit=label_combo_limit,
                )
                self._histograms[name] = h
            return h

    def render_prometheus(self) -> str:
        parts: list[str] = []
        with self._lock:
            for c in self._counters.values():
                parts.extend(c.render())
            for gauge in self._gauges.values():
                parts.extend(gauge.render())
            for h in self._histograms.values():
                parts.extend(h.render())
        parts.append("")  # Trailing newline
        return "\n".join(parts)

    def counter_items(self, name: str) -> list[tuple[dict[str, str], float]]:
        """某 counter 的当前 (labels, value) 快照；不存在返 []。

        credential-readiness R8b：进程内观测面读取（单机部署，onboarding
        投影直读 refusal counter，无需绕 /metrics 文本再解析）。
        """
        with self._lock:
            c = self._counters.get(name)
            if c is None:
                return []
            return [(dict(key), v) for key, v in c._values.items()]

    def reset(self) -> None:
        """Test helper —— 别在生产调."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


_REGISTRY: MetricsRegistry | None = None
_REG_LOCK = threading.Lock()


def get_registry() -> MetricsRegistry:
    global _REGISTRY
    with _REG_LOCK:
        if _REGISTRY is None:
            _REGISTRY = MetricsRegistry()
        return _REGISTRY


# ─────────────────────────────────────────────────────────────
# Namespace convenience accessors (less noise at call sites)
# ─────────────────────────────────────────────────────────────
def inc(
    name: str,
    labels: dict[str, str] | None = None,
    value: float = 1.0,
    help: str = "",  # noqa: A002
    label_combo_limit: int = LABEL_COMBO_LIMIT,
) -> None:
    get_registry().counter(
        name,
        help=help,
        label_combo_limit=label_combo_limit,
    ).inc(value=value, labels=labels)


def observe(name: str, value: float, labels: dict[str, str] | None = None, help: str = "", buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:  # noqa: A002
    get_registry().histogram(name, help=help, buckets=buckets).observe(
        value=value,
        labels=labels,
    )


def set_gauge(
    name: str,
    value: float,
    labels: dict[str, str] | None = None,
    help: str = "",  # noqa: A002
    label_combo_limit: int = LABEL_COMBO_LIMIT,
) -> None:
    get_registry().gauge(
        name,
        help=help,
        label_combo_limit=label_combo_limit,
    ).set(value=value, labels=labels)


def counter_items(name: str) -> list[tuple[dict[str, str], float]]:
    """模块级便利：`get_registry().counter_items(name)`。"""
    return get_registry().counter_items(name)
