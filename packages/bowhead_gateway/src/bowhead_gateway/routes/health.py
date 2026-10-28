"""健康检查端点。

- ``/health``       — Liveness（进程活着）
- ``/readyz``       — Readiness（依赖就绪：DB / Redis / Chroma / 至少一个 LLM provider 可达）
- ``/metrics``      — Prometheus text exposition（#107，默认 off，env 开启）
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from bowhead_gateway.terminal_protocol_rollout import (
    DurableChatExecutionMode,
    durable_chat_execution_config,
    fresh_terminal_v1_admission_enabled,
)

router = APIRouter()

logger = logging.getLogger(__name__)


def _read_version() -> str:
    # Single source: VERSION file, copied by Dockerfile to /app/VERSION;
    # also injectable via BOWHEAD_VERSION env (build arg). Avoid hardcoded versions missed during updates.
    env_v = os.environ.get("BOWHEAD_VERSION", "").strip()
    if env_v:
        return env_v
    for p in ("/app/VERSION", Path(__file__).parent / "../../../../../VERSION"):
        try:
            with Path(p).open(encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return "dev"


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: int
    # Sprint θ Phase 8d P1: minimum frontend version the backend expects.
    # ThetaCacheBustBanner polls /health every 60s + compares against
    # NEXT_PUBLIC_BUILD_VERSION; mismatch -> new-version-available pill. Without this
    # field the banner never fires and stale browsers keep running old code.
    # Read from BOWHEAD_FRONTEND_MIN env (set in deploy/.env when shipping a
    # frontend that drops backwards compat); default = current backend version
    # so dev + freshly-shipped prod both don't trigger the banner.
    frontend_min: str | None = None
    # Forced reload override — bypasses user's 24h dismiss. Trip via
    # BOWHEAD_FORCE_FRONTEND_RELOAD=1 for emergency push (e.g. hotfix that
    # corrupts stale clients).
    force_reload: bool = False
    # Retain only browser-required runtime capabilities; backend rollout flags are outside public health contract.
    feature_flags: dict[str, bool] = {}
    # Safe runtime diagnostics only: no secrets, tokens, internal URLs, host paths or integrity digests.
    runtime_info: dict[str, str | bool | None] = {}


class ReadyResponse(BaseModel):
    status: str
    checks: dict[str, str]


def _read_feature_flags() -> dict[str, bool]:
    """返回浏览器需要读取的运行时能力。"""

    return {
        "session_archive": True,
        "error_diagnostics": os.environ.get("FEATURE_ERROR_DIAGNOSTICS", "1") == "1",
        "results_workspace": True,
    }


def _wheelhouse_present() -> bool:
    """Return whether the shared offline dependency directory is available."""
    path = (os.environ.get("BOWHEAD_SANDBOX_WHEELHOUSE") or "").strip()
    if not path:
        install_dir = (os.environ.get("BOWHEAD_INSTALL_DIR") or "").strip()
        if install_dir:
            path = str(Path(install_dir) / "wheelhouse")
    return bool(path and Path(path).is_dir())


@lru_cache(maxsize=1)
def _sandbox_workdir_probe() -> tuple[str, bool | None]:
    """共享沙箱 workdir 根的**可用性**，给运维做部署验收（`(effective_root, writable)`）。

    探针会解析有效共享根，并真去建一个探测目录再删掉：`root:root 0755` 能过存在性
    检查，但 app（uid 1000）写不进去，python_sandbox 会 100% 失败。公开接口只返回
    可写性，不返回解析后的宿主机路径。

    `writable=None` = 根本没解析出共享根（本机开发常态，走系统 /tmp，不是故障）。

    **memoized**：executor 因 compose `depends_on: service_healthy` 必然在 app 启动前
    把该目录准备好，故首次观测即代表部署姿态；不做每次 /health 探测，避免 Docker
    healthcheck 每 30s 往 bind mount 写一次。

    **绝不抛**（观测位，永不承重）。`/health` 是 compose
    healthcheck 的落点，这里漏一个异常 = 容器永远不 healthy = 整个部署卡住，而代价只是
    一个诊断字段。`bowhead_skill` 还不是 bowhead_gateway 的声明依赖（单 venv 镜像里能解析，
    但只装 gateway 的装法会 ImportError），故 import 也一并罩进来。
    """
    try:
        from bowhead_skill.runtime import _resolve_workdir_root, _workdir_root_unusable_reason

        root = _resolve_workdir_root()
        if not root:
            return ("", None)
        return (root, _workdir_root_unusable_reason(root) is None)
    except Exception:  # noqa: BLE001 — Observation only; failed probe means unresolved shared root.
        logger.exception("health.sandbox_workdir_probe_failed")
        return ("", None)


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe — 只要进程活着就返回 OK。"""
    backend_v = _read_version()
    fe_min = os.environ.get("BOWHEAD_FRONTEND_MIN", "").strip() or backend_v
    force = (os.environ.get("BOWHEAD_FORCE_FRONTEND_RELOAD") or "0").strip() == "1"
    return HealthResponse(
        status="ok",
        version=backend_v,
        timestamp=int(time.time()),
        frontend_min=fe_min,
        force_reload=force,
        feature_flags=_read_feature_flags(),
        runtime_info=_health_runtime_info(),
    )


def _health_runtime_info() -> dict[str, str | bool | None]:
    runtime_info: dict[str, str | bool | None] = {
        "sandbox_workdir_writable": _sandbox_workdir_probe()[1],
        "sandbox_executor_url_configured": bool((os.environ.get("BOWHEAD_SANDBOX_EXECUTOR_URL") or "").strip()),
        "sandbox_executor_token_configured": bool((os.environ.get("BOWHEAD_SANDBOX_EXECUTOR_TOKEN") or "").strip()),
        "wheelhouse_present": _wheelhouse_present(),
    }
    return runtime_info


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    """Prometheus text format 0.0.4 —— 默认关，env `BOWHEAD_METRICS_ENDPOINT_ENABLED=1` 打开.

    设计见 docs/metrics-outlet-design.md. v1 不做鉴权，建议部署侧走内网 scraper.
    """
    if (os.environ.get("BOWHEAD_METRICS_ENDPOINT_ENABLED") or "0") != "1":
        raise HTTPException(status_code=404, detail="metrics endpoint disabled")
    from bowhead_core.metrics import get_registry

    body = get_registry().render_prometheus()
    return PlainTextResponse(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/readyz", response_model=ReadyResponse)
async def ready(request: Request, response: Response) -> ReadyResponse:
    """Readiness probe — M1 会真正检查依赖。"""
    # Sprint 0 stub; M1 checks:
    #   - DB: SELECT 1
    #   - Redis: PING
    #   - Chroma: heartbeat
    #   - At least one LLM provider circuit breaker closed.
    checks = {
        "db": "stub",
        "redis": "stub",
        "chroma": "stub",
        "llm": "stub",
    }
    compatibility_heartbeat = getattr(
        request.app.state,
        "subagent_compatibility_heartbeat",
        None,
    )
    compatibility_state = getattr(
        compatibility_heartbeat,
        "state",
        None,
    )
    if compatibility_state is not None:
        checks["subagent_compatibility"] = "ready" if compatibility_state.status == "ready" else "unavailable"
    from bowhead_core.subagent_management_flags import (
        subagent_management_v2_ready,
    )

    marketplace_ready = await subagent_management_v2_ready(
        getattr(request.app.state, "agent_assignment_store", None),
        schema_ready=bool(
            getattr(request.app.state, "agent_marketplace_schema_ready", False),
        ),
    )
    checks["agent_marketplace"] = "ready" if marketplace_ready else "unavailable"
    # Submission recovery is retained only as a compatibility substrate. When
    # new browser submissions are disabled, optional key/schema readiness must
    # not remove an otherwise healthy chat instance from rotation.
    from bowhead_core.composer_rollout import (
        composer_deployment_projection,
        composer_submission_protocol_enabled,
        composer_submission_protocol_ready,
        submission_fingerprint_keyring,
        submission_fingerprint_previous_accept_until,
    )

    projection = composer_deployment_projection()
    checks["composer_flag_matrix"] = "ready" if projection.matrix_valid else "unavailable"
    session_store = getattr(request.app.state, "session_store", None)
    if composer_submission_protocol_enabled():
        checks["composer_submission_key"] = "ready" if composer_submission_protocol_ready() else "unavailable"
        schema_ready = False
        if session_store is not None and hasattr(
            session_store,
            "chat_submission_protocol_schema_ready",
        ):
            try:
                schema_ready = bool(await session_store.chat_submission_protocol_schema_ready())
            except Exception:
                schema_ready = False
        checks["composer_submission_schema"] = "ready" if schema_ready else "unavailable"
        key_epochs_ready = False
        if session_store is not None and hasattr(
            session_store,
            "chat_submission_fingerprint_keyring_aligned",
        ):
            try:
                key_epochs_ready = bool(
                    await session_store.chat_submission_fingerprint_keyring_aligned(
                        configured_key_ids=tuple(key_id for key_id, _ in submission_fingerprint_keyring()),
                        previous_accept_until=submission_fingerprint_previous_accept_until(),
                    )
                )
            except Exception:
                key_epochs_ready = False
        checks["composer_submission_key_epochs"] = "ready" if key_epochs_ready else "unavailable"
    else:
        checks["composer_submission_key"] = "inactive"
        checks["composer_submission_schema"] = "inactive"
        checks["composer_submission_key_epochs"] = "inactive"
    checks["team_model_boundary"] = "ready" if getattr(request.app.state, "team_model_boundary_ready", False) is True else "unavailable"
    terminal_store = getattr(
        request.app.state,
        "terminal_protocol_store",
        None,
    )
    terminal_config = durable_chat_execution_config()
    terminal_fresh_writer_configured = fresh_terminal_v1_admission_enabled(
        terminal_config,
    )
    service = getattr(request.app.state, "terminal_writer_service", None)
    sweep = getattr(request.app.state, "terminal_recovery_sweep", None)
    terminal_factory = getattr(
        request.app.state,
        "terminal_runtime_factory",
        None,
    )
    terminal_generation_buses = getattr(
        request.app.state,
        "terminal_generation_bus_registry",
        None,
    )
    terminal_recovery = getattr(
        request.app.state,
        "terminal_recovery_processor",
        None,
    )
    terminal_server_bundle_ready = bool(
        terminal_store is not None
        and all(
            callable(getattr(terminal_store, method, None))
            for method in (
                "admit_or_steer_v1",
                "read_execution_accepted_highwater",
                "read_web_chat_action",
            )
        )
        and terminal_factory is not None
        and all(
            callable(getattr(terminal_factory, method, None))
            for method in (
                "admit_web_turn",
                "steer_web_turn",
                "wake_admitted_web_turn",
            )
        )
        and callable(getattr(terminal_generation_buses, "get_or_create", None))
        and callable(getattr(terminal_recovery, "run_bounded", None))
        and service is not None
        and (not terminal_fresh_writer_configured or getattr(service, "startup_recovery_ready", False))
        and sweep is not None
        and not getattr(sweep, "errors", ())
    )
    checks["terminal_server_bundle"] = "ready" if terminal_server_bundle_ready else "unavailable"
    if terminal_config.mode is DurableChatExecutionMode.INVALID:
        checks["durable_chat_execution_config"] = "unavailable"
        checks["terminal_writer"] = "unavailable"
        checks["terminal_recovery"] = "unavailable"
    elif terminal_config.mode in {
        DurableChatExecutionMode.OFF,
        DurableChatExecutionMode.SHADOW,
    }:
        checks["durable_chat_execution_config"] = "ready"
        checks["terminal_writer"] = "inactive"
        checks["terminal_recovery"] = "ready" if terminal_server_bundle_ready else "unavailable"
    else:
        checks["durable_chat_execution_config"] = "ready"
        substrate_ready = bool(terminal_fresh_writer_configured and terminal_server_bundle_ready and getattr(service, "fresh_admission_ready", False))
        checks["terminal_writer"] = "ready" if substrate_ready else "unavailable"
        checks["terminal_recovery"] = "ready" if terminal_server_bundle_ready else "unavailable"
    # Export capability participates in readiness only where a runner **was assembled**.
    # Deployments without runners (images lacking node/soffice/pdftoppm, unit-test environments) do not offer export;
    # blocking whole-Gateway readiness would misreport no-export deployments as broken.
    # canvas_export.v2_capabilities startup logs separately record assembly; visibility does not depend on this check.
    runner = getattr(request.app.state, "export_runner", None)
    if runner is not None:
        for fmt, profile in (
            ("pdf", "full_report"),
            ("pptx", "full_report"),
            ("pptx", "executive_summary"),
            ("xlsx", "data_workbook"),
        ):
            available = bool(runner.is_available_for(fmt, profile))
            checks[f"canvas_export:{fmt}/{profile}"] = "ready" if available else "unavailable"

    status = "not_ready" if any(value == "unavailable" for value in checks.values()) else "ready"
    if status == "not_ready":
        response.status_code = 503
    return ReadyResponse(status=status, checks=checks)
