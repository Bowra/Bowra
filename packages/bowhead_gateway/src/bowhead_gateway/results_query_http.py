"""Finite HTTP errors and low-cardinality metrics for Results queries."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import HTTPException

from bowhead_gateway.results_contracts import ResultsQueryResponseV1

ResultsScopeLabel = Literal["owner", "report_only"]


def coded_results_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code}},
        headers={
            "Cache-Control": "private, no-store",
            "Vary": "Cookie",
        },
    )


def _scale_bucket(total: int | None) -> str:
    if total is None:
        return "unknown"
    if total == 0:
        return "0"
    if total < 30:
        return "1_29"
    if total < 100:
        return "30_99"
    if total < 500:
        return "100_499"
    if total < 2_000:
        return "500_1999"
    return "2000_plus"


def record_results_query_metrics(
    *,
    started_at: float,
    scope: ResultsScopeLabel,
    surface: str,
    error_class: str,
    result: ResultsQueryResponseV1 | None = None,
) -> None:
    try:
        from bowhead_core import metrics

        scale_bucket = _scale_bucket(
            result.authorized_total if result is not None and surface != "changes" else None,
        )
        labels = {
            "scope": scope,
            "surface": surface,
            "status": "ok" if result is not None else "error",
            "error_class": error_class,
            "scale_bucket": scale_bucket,
        }
        metrics.inc("results_query_requests_total", labels=labels)
        metrics.observe(
            "results_query_duration_seconds",
            time.perf_counter() - started_at,
            labels={
                "scope": scope,
                "surface": surface,
                "scale_bucket": scale_bucket,
            },
        )
        if error_class == "results_snapshot_stale":
            metrics.inc(
                "results_snapshot_stale_total",
                labels={
                    "scope": scope,
                    "reason_class": "binding_or_projection",
                },
            )
        if result is not None:
            status = result.projection_status
            if status.report_relations == "unavailable":
                metrics.inc(
                    "results_projection_fallback_total",
                    labels={"relation_kind": "report"},
                )
            if status.execution_relations == "unavailable":
                metrics.inc(
                    "results_projection_fallback_total",
                    labels={"relation_kind": "execution"},
                )
            if status.export_relations == "degraded":
                metrics.inc(
                    "results_projection_fallback_total",
                    labels={"relation_kind": "export"},
                )
    except Exception:  # noqa: S110 - observability must never break reads
        pass


__all__ = ["coded_results_error", "record_results_query_metrics"]
