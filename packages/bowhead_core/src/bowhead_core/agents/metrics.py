"""Agent 系统 Prometheus metrics + alerting rules (sprint multi-agent-v1 PR-#13).

实施 sprint-multi-agent.md §9.

封装 bowhead_core.metrics.inc / observe 的 type-safe wrapper, 提供:
- 统一 metric 名 (常量, 防 typo)
- per-event helper (emit_dispatch / emit_task_failed / ...)
- 全套 alerting rules 定义 (供 SRE 配 Prometheus alertmanager)

不直接接 Prometheus scraper / dashboard (那是 ops 职责).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bowhead_core import metrics as core_metrics

# -- Metric names (T-SRE-P1.2 alerting rules use the same keys) --

DISPATCH_TOTAL = "bowhead_multi_agent_dispatch_total"
PARALLEL_LATENCY_MS = "bowhead_multi_agent_parallel_latency_ms"
TASK_FAILED_TOTAL = "bowhead_multi_agent_task_failed_total"
PARTIAL_SUCCESS_TOTAL = "bowhead_multi_agent_partial_success_total"
CANVAS_WRITER_FABRICATE_DETECTED_TOTAL = "bowhead_canvas_writer_fabricate_detected_total"
INTENT_CLASSIFIED_TOTAL_V2 = "bowhead_intent_classified_total_v2"
REDIS_HEALTH_STATUS = "redis_health_status"
H2_APPROVAL_TIMEOUT_TOTAL = "bowhead_h2_approval_timeout_total"
H2_APPROVAL_OUTCOME_TOTAL = "bowhead_h2_approval_outcome_total"
MANIFEST_LOAD_FAIL_TOTAL = "bowhead_manifest_load_fail_total"
MANIFEST_UPLOAD_TOTAL = "bowhead_manifest_upload_total"

# Sprint θ pre-discovery: legacy meta-intent fallback counter (2026-05-08).
THETA_LEGACY_META_INTENT_TOTAL = "bowhead_theta_legacy_meta_intent_total"


# -- Type aliases --

ComplexityLabel = Literal["low", "med", "high"]
IntentLabel = Literal["meta", "work", "synthesize", "chat", "config"]
H2OutcomeLabel = Literal["confirm", "edit", "cancel", "timeout"]
RedisHealthLabel = Literal[0, 1, 2]  # 0=ok, 1=degraded, 2=down

# Legacy meta_intent labels (8-way enum).
LegacyMetaLabel = Literal[
    "canvas_init",
    "canvas_append",
    "canvas_patch",
    "canvas_structure",
    "canvas_export",
    "content_only",
    "ambiguous_hitl",
    "kb_search",
]


# -- emit helpers --


def emit_dispatch(*, complexity: ComplexityLabel, agent_count: int) -> None:
    """每次 dispatch_parallel 调用时 emit."""
    core_metrics.inc(
        DISPATCH_TOTAL,
        labels={
            "complexity": complexity,
            "agent_count": str(min(agent_count, 10)),  # Cap at 10 to bound cardinality.
        },
    )


def emit_parallel_latency(*, complexity: ComplexityLabel, agent_count: int, latency_ms: float) -> None:
    """parallel branch 完成时 emit P50/P95/P99 用."""
    core_metrics.observe(
        PARALLEL_LATENCY_MS,
        latency_ms,
        labels={
            "complexity": complexity,
            "agent_count": str(min(agent_count, 10)),
        },
    )


def emit_task_failed(*, agent_name: str, reason: str) -> None:
    """单 task fail 时 emit. reason 是 TaskFailureError.reason (短分类)."""
    core_metrics.inc(
        TASK_FAILED_TOTAL,
        labels={
            "agent": agent_name[:64],  # Cap to bound cardinality.
            "reason": reason[:32],
        },
    )


def emit_partial_success(*, n_failed: int, n_total: int) -> None:
    """N 个 task 中 X 个 fail 时 emit (X > 0 且 X < n_total)."""
    if n_total == 0:
        return
    ratio_label = f"{n_failed}/{n_total}"
    core_metrics.inc(
        PARTIAL_SUCCESS_TOTAL,
        labels={"ratio": ratio_label[:16]},
    )


def emit_canvas_writer_fabricate(*, agent: str = "canvas-writer") -> None:
    """canvas-writer 输出 fabricate 数字被 output_schema reject 时 emit (P0 alert)."""
    core_metrics.inc(
        CANVAS_WRITER_FABRICATE_DETECTED_TOTAL,
        labels={"agent": agent[:64]},
    )


def emit_intent_classified(*, intent: IntentLabel) -> None:
    """5-way fast path 分类完成时 emit."""
    core_metrics.inc(
        INTENT_CLASSIFIED_TOTAL_V2,
        labels={"intent": intent},
    )


def emit_theta_legacy_meta_intent(*, meta: str) -> None:
    """Legacy meta_intent classifier 跑时 emit (= v2 fallback to legacy 计数).

    Sprint θ discovery: 此 metric 增长速率反映 v2 hook return None 的频率.
    高 = legacy path 还在大量服务用户; 低 = v2 已 cover 大部分.
    """
    core_metrics.inc(
        THETA_LEGACY_META_INTENT_TOTAL,
        labels={"meta": meta[:32]},
    )


def emit_redis_health(status: RedisHealthLabel) -> None:
    """RedisHealthMonitor 周期性 emit. 0=ok, 1=degraded, 2=down."""
    core_metrics.observe(
        REDIS_HEALTH_STATUS,
        float(status),
        labels={},
    )


def emit_h2_approval_outcome(*, outcome: H2OutcomeLabel) -> None:
    """TaskPlanCard 用户决策完成时 emit. (UX issue 看 timeout 占比)."""
    core_metrics.inc(
        H2_APPROVAL_OUTCOME_TOTAL,
        labels={"outcome": outcome},
    )


def emit_manifest_load_fail(*, source: Literal["system", "user"], reason: str) -> None:
    """manifest 启动 / 上传 load 失败时 emit (data integrity P2 alert)."""
    core_metrics.inc(
        MANIFEST_LOAD_FAIL_TOTAL,
        labels={"source": source, "reason": reason[:32]},
    )


def emit_manifest_upload(*, status: Literal["success", "rejected", "error"]) -> None:
    """admin upload endpoint 调用 outcome (rate limit / lint reject 等)."""
    core_metrics.inc(
        MANIFEST_UPLOAD_TOTAL,
        labels={"status": status},
    )


# -- Alerting rules (T-SRE-P1.2) --
# Static configuration for operations to copy directly into Prometheus alertmanager rules.yaml.


@dataclass(frozen=True)
class AlertingRule:
    """单 alert 规则定义 (Prometheus alertmanager 兼容)."""

    name: str
    metric: str
    threshold: str  # PromQL comparison expression, e.g. "X > 5".
    window: str  # "5m" / "1m"
    page_level: Literal["P0", "P1", "P2", "P3"]
    description: str


ALERTING_RULES: list[AlertingRule] = [
    AlertingRule(
        name="MultiAgentTaskFailedHigh",
        metric=TASK_FAILED_TOTAL,
        threshold="rate(bowhead_multi_agent_task_failed_total[5m]) > 5/60",
        window="5m",
        page_level="P2",
        description="task fail rate > 5/min for 5 min",
    ),
    AlertingRule(
        name="MultiAgentParallelLatencyHigh",
        metric=PARALLEL_LATENCY_MS,
        threshold="histogram_quantile(0.95, bowhead_multi_agent_parallel_latency_ms_bucket) > 80000",
        window="5m",
        page_level="P1",
        description="P95 parallel branch latency > 80s for 5 min",
    ),
    AlertingRule(
        name="CanvasWriterFabricateDetected",
        metric=CANVAS_WRITER_FABRICATE_DETECTED_TOTAL,
        threshold="increase(bowhead_canvas_writer_fabricate_detected_total[1m]) >= 1",
        window="1m",
        page_level="P0",
        description="canvas-writer fabricate detected — immediate page",
    ),
    AlertingRule(
        name="MultiAgentPartialSuccessHigh",
        metric=PARTIAL_SUCCESS_TOTAL,
        threshold=("rate(bowhead_multi_agent_partial_success_total[10m]) / rate(bowhead_multi_agent_dispatch_total[10m]) > 0.3"),
        window="10m",
        page_level="P2",
        description="partial success rate > 30% for 10 min",
    ),
    AlertingRule(
        name="RedisHealthDown",
        metric=REDIS_HEALTH_STATUS,
        threshold="redis_health_status == 2",
        window="instant",
        page_level="P1",
        description="redis health status = down (CeleryTaskRunner fallback)",
    ),
    AlertingRule(
        name="ManifestUploadErrorHigh",
        metric=MANIFEST_UPLOAD_TOTAL,
        threshold='rate(bowhead_manifest_upload_total{status=~"rejected|error"}[5m]) > 10/60',
        window="5m",
        page_level="P3",
        description="manifest upload error rate > 10/min — UX issue ticket",
    ),
    AlertingRule(
        name="H2ApprovalTimeoutHigh",
        metric=H2_APPROVAL_OUTCOME_TOTAL,
        threshold=('rate(bowhead_h2_approval_outcome_total{outcome="timeout"}[30m]) / rate(bowhead_h2_approval_outcome_total[30m]) > 0.4'),
        window="30m",
        page_level="P3",
        description="H2 approval timeout rate > 40% for 30 min — UX issue",
    ),
    AlertingRule(
        name="ManifestLoadFail",
        metric=MANIFEST_LOAD_FAIL_TOTAL,
        threshold="increase(bowhead_manifest_load_fail_total[1m]) >= 1",
        window="1m",
        page_level="P2",
        description="manifest load failed — data integrity issue",
    ),
]


def to_prometheus_yaml() -> str:
    """生成 Prometheus alertmanager rules.yaml 片段 (供 ops 直接复制)."""
    lines = ["groups:", "  - name: bowhead_multi_agent_alerts", "    rules:"]
    for r in ALERTING_RULES:
        lines.extend(
            [
                f"      - alert: {r.name}",
                f"        expr: {r.threshold}",
                f"        for: {r.window}" if r.window != "instant" else "        for: 0s",
                "        labels:",
                f"          severity: {r.page_level}",
                "        annotations:",
                f"          summary: {r.description!r}",
                f"          metric: {r.metric}",
            ]
        )
    return "\n".join(lines) + "\n"
