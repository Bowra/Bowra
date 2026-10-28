"""Sprint χ feature flags — env var pattern (mirrors psi_flags / provenance_flags).

Reads env on every access (cheap dict lookup) so a future /admin/flags endpoint
that mutates os.environ would take effect without process restart. Today, prod
flip-ON is via docker-compose env change + `up -d --force-recreate api`
(~1 min downtime, same standard as ψ shipped 0.14.1).

**Single-worker requirement** (I-χ-SW1, enforced by assert_consistent_and_single_worker
at lifespan startup): future os.environ mutation only propagates within this
process; never fork() after mutating env, and never enable WEB_CONCURRENCY>1
without redesigning to distributed flag storage (Redis / SQLite row pattern).

Defaults: PARALLEL_EMIT, PARALLEL_EXEC and BI_BATCH are enabled.
Parallel-tool prompt guidance is part of the current prompt builders.
Set `BOWHEAD_CHI_<NAME>=0` to disable any flag (rollback path).

Flags:
- PARALLEL_EMIT — OpenAI adapter set body["parallel_tool_calls"]=true
- PARALLEL_EXEC — subagent gather _execute_tool concurrently
- BI_BATCH — bi_query schema 暴露 queries: List[str] + 内部 fan-out

Invariant I-χ-SW1 (tech-design-r3 §0.3): single worker required.
"""

from __future__ import annotations

import os

_FLAG_DEFAULTS: dict[str, str] = {
    "CHI_PARALLEL_EMIT": "1",  # χ-1 ship-default ON: provider parallel_tool_calls=true (sniff fallback safe)
    "CHI_PARALLEL_EXEC": "1",  # χ-2 ship-default ON: subagent gather + double semaphore
    "CHI_BI_BATCH": "1",  # χ-3 ship-default ON: bi_query queries=[..] batch + gather fan-out
}


def get(name: str) -> bool:
    """Read flag from env each call. `name` is short suffix (e.g. 'PARALLEL_EMIT').

    Matches psi_flags pattern — re-evaluates per call so future mutation of
    os.environ takes effect without restart.
    """
    key = f"CHI_{name}"
    default = _FLAG_DEFAULTS.get(key, "0")
    return os.environ.get(f"BOWHEAD_{key}", default) == "1"


def parallel_emit_enabled() -> bool:
    return get("PARALLEL_EMIT")


def parallel_exec_enabled() -> bool:
    return get("PARALLEL_EXEC")


def bi_batch_enabled() -> bool:
    return get("BI_BATCH")


def max_concurrent() -> int:
    """Per-batch gather semaphore size."""
    return int(os.environ.get("BOWHEAD_CHI_MAX_CONCURRENT", "6"))


def global_max_concurrent() -> int:
    """Process-wide cross-turn gather semaphore size."""
    return int(os.environ.get("BOWHEAD_CHI_GLOBAL_MAX_CONCURRENT", "12"))


def all_flags_dict() -> dict[str, bool]:
    """For /health endpoint exposure (mirrors psi_flags.all_flags_dict)."""
    return {k.lower(): get(k.removeprefix("CHI_")) for k in _FLAG_DEFAULTS}


def assert_consistent_and_single_worker() -> None:
    """App startup gate (lifespan).

    1. WEB_CONCURRENCY != 1 → abort (I-χ-SW1 invariant; module-level state assumes
       single worker; multi-worker requires distributed coordination — separate sprint).
    2. BI_BATCH=1 + PARALLEL_EXEC=0 → abort (batch tool internal fan-out needs gather).
    """
    web_concur = os.environ.get("WEB_CONCURRENCY", "1")
    if web_concur != "1":
        raise RuntimeError(f"Sprint χ requires WEB_CONCURRENCY=1 (single worker). Got WEB_CONCURRENCY={web_concur!r}. See tech-design-r3.md §0.3.")
    if bi_batch_enabled() and not parallel_exec_enabled():
        raise RuntimeError("BOWHEAD_CHI_BI_BATCH=1 requires BOWHEAD_CHI_PARALLEL_EXEC=1 (batch tool internal fan-out needs gather).")
