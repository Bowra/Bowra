"""FastAPI 主入口。

LLM Provider 及其凭据由团队设置保存；进程启动时只构建不可调用外部服务的
mock Router。真实请求必须在请求期从团队配置构建专属 Router。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import shutil
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite
import anyio
import structlog
from bowhead_core.datarc_connection_store import DatarcConnectionStore
from bowhead_core.env_utils import env_float
from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from bowhead_bi_bridge import DatarcClientFactory
from bowhead_core import (
    ApprovalStore,
    ArtifactStore,
    CanvasStore,
    Orchestrator,
    OrchestratorConfig,
    OrderedTurnLogSink,
    PersonalMemoryGovernanceStore,
    SessionStore,
    TurnLogStore,
    UserMemoryStore,
    build_default_registry,
    get_event_registry,
)
from bowhead_gateway.product_identity import product_name
from bowhead_gateway.routes import (
    approvals as approvals_routes,
)
from bowhead_gateway.routes import (
    canvas as canvas_routes,
)
from bowhead_gateway.routes import (
    chat,
    files,
    health,
    memory,
    sessions,
)
from bowhead_gateway.routes import (
    clarifications as clarifications_routes,
)
from bowhead_gateway.routes import (
    datarc as datarc_routes,
)
from bowhead_gateway.routes import (
    feishu as feishu_routes,
)
from bowhead_gateway.routes import (
    home as home_routes,
)
from bowhead_gateway.routes import (
    home_proposals as home_proposal_routes,
)
from bowhead_gateway.routes import (
    memory_candidates as memory_candidates_routes,
)
from bowhead_gateway.routes import (
    notifications as notifications_routes,
)
from bowhead_gateway.routes import (
    skills as skills_routes,
)
from bowhead_gateway.routes import (
    slack as slack_routes,
)
from bowhead_gateway.routes import (
    turns as turns_routes,
)
from bowhead_gateway.settings import get_settings
from bowhead_gateway.terminal_protocol_rollout import (
    durable_chat_execution_config,
    fresh_terminal_v1_admission_enabled,
)
from bowhead_memory import (
    DreamingLight,
    EmbedProxyClientFactory,
    MemoryCandidateStore,
    VectorClient,
    VectorDBError,
)
from bowhead_router import AgentBinding, ProviderConfig, ProviderRegistry, Router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _legacy_untracked_orphan_min_age_minutes() -> int | None:
    """Return an age fence beyond every supported legacy Lead execution."""

    hard_cap_s = env_float("BOWHEAD_LEAD_LOOP_HARD_TIMEOUT_SEC", 1800.0)
    if not math.isfinite(hard_cap_s):
        return None
    return max(60, math.ceil((2 * hard_cap_s) / 60))


def _configure_logging(log_level: str, log_format: str) -> None:
    logging.basicConfig(level=log_level.upper())
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, log_level.upper())),
        cache_logger_on_first_use=True,
    )


def _load_dotenv_into_process(log: structlog.types.FilteringBoundLogger) -> None:
    """Load local `.env` into `os.environ` for non-Settings consumers."""

    try:
        from dotenv import load_dotenv
    except Exception as exc:  # pragma: no cover - dependency should exist via pydantic-settings
        log.warning("dotenv.import_failed", err=str(exc))
        return
    loaded = load_dotenv(".env", override=False)
    if loaded:
        log.info("dotenv.loaded_for_process", path=".env")


_ASSEMBLY_BOUND_AGENT_TYPES = ("lead", "modeler", "analyst", "echo", "judge")


class _AssemblyRouter(Router):
    """装配占位 Router：**任何** agent_type 都能 resolve 到装配 binding。

    批 2-a §3.2 ①。基类 `_resolve_key` 对未绑的 agent_type 抛裸 `KeyError`，而
    `KeyError` 在 adapter 之前就发生 —— 于是 `RefusalAdapter._refuse` 的
    `record_refusal()` **从未触发**（站点 7 的 `home_proposal` 就是这个形状）。
    `bowhead_llm_refusal_total` 因此「零值有两种含义」，不能当判据。

    catch-all 之后，未绑的 agent_type 也会落到装配 adapter，refusal 得到记账，
    失败形态与已绑的那几个统一。

    ★ 为什么**不**把拒绝上移到 `resolve()`（r3 review 实跑对照证实）：
    `Router.invoke`/`stream` 内部都调 `self.resolve(...)`，在那里抛会绕过整台
    fallback 机器 → 异常从 `InvocationTargetsExhaustedError` 变成裸
    `TeamModelUnconfiguredError` → `project_llm_failure` 只认前者、返 None →
    chat 的 `reason_code` 从 `llm_provider_unavailable` 退化成
    `lead_internal_error`，带 provider/kind/retryable/occurrence_id 的结构化日志
    整段丢失。**这条改动想买的正是归因，上移 resolve 恰好把它弄丢。**
    """

    def _resolve_key(self, agent_type: str, profile: str | None) -> tuple[str, str | None]:
        try:
            return super()._resolve_key(agent_type, profile)
        except KeyError:
            # Unbound → catch-all, so rejection happens in the adapter (accountable, exception classification unchanged).
            return (_ASSEMBLY_BOUND_AGENT_TYPES[0], None)


def _assembly_router_is_mock() -> bool:
    """装配 Router 用 mock 还是 refusal —— **默认 refusal**（批 2-a §3.2 ②）。

    此前这个选择挂在 `BOWHEAD_ENV` 上。那是缺陷：一个与部署环境无关的**安全**
    语义，被绑在一个各箱各写的变量上——116 实测就写成了 `dev`，于是「prod 装
    RefusalAdapter」这个所有人都以为成立的前提在真机上不成立，所有旁路静默吃
    MockAdapter 的 echo，refusal 指标恒 0。

    现在必须**显式声明**才给 mock：本地离线开发与需要 echo 的少数测试自己设
    `BOWHEAD_ASSEMBLY_ROUTER=mock`。任何环境写错 `BOWHEAD_ENV` 都不再静默退化。
    CI / 单测**不要**全局声明——那会让整套测试跑在 echo 上，新增旁路在闸内静默
    通过，守卫就只剩生产告警、零 pre-ship 信号。
    """
    return (os.environ.get("BOWHEAD_ASSEMBLY_ROUTER") or "").strip().lower() == "mock"


def _build_assembly_router(_env: str | None = None) -> tuple[Router, ProviderRegistry]:
    """进程级装配 Router（credential-readiness D1 + 批 2-a §3.2）。

    平台不持有部署级模型密钥；这个 Router 仅供缺少团队上下文的内部装配。
    默认 RefusalAdapter（被真正调用即抛 `TeamModelUnconfiguredError` 并经 refusal
    sink 归因——任何走到它的调用都说明某条旁路漏接团队 router）；显式
    `BOWHEAD_ASSEMBLY_ROUTER=mock` 才给 MockAdapter（离线开发）。

    `_env` 参数保留只为兼容既有调用点签名，**不再参与选型**（见
    `_assembly_router_is_mock` 的说明）。
    """
    mock = _assembly_router_is_mock()
    provider_name = "llm_mock" if mock else "llm_refusal"
    registry = ProviderRegistry()
    registry.load([ProviderConfig(name=provider_name, type="mock" if mock else "refusal")])
    router = _AssemblyRouter(registry)
    for agent_type in _ASSEMBLY_BOUND_AGENT_TYPES:
        router.bind(
            AgentBinding(
                agent_type=agent_type,
                primary=(provider_name, "mock-model"),
            )
        )
    return router, registry


_LOCAL_PRESIGN_SECRET_MIN_LEN = 32


def _resolve_local_presign_secret(settings) -> str | None:  # type: ignore[no-untyped-def]
    """Sprint file-upload-presign Phase 1.2.

    Resolve the HS256 secret used to sign local-receiver JWTs.
    Source order:
      1. settings.local_presign_secret (BOWHEAD_LOCAL_PRESIGN_SECRET env) —
         REQUIRED ≥32 chars; refuses to boot below (review P1-S-2).
      2. auto-generated per-process via secrets.token_urlsafe(32) (43 chars).
    Returns ``None`` when ``allow_local_receiver=False`` so the LocalBackend
    cleanly disables the presign path.

    **Call this ONCE per lifespan** (review P0-S-1) and reuse the value for
    both LocalBackend.local_presign_secret and app.state.local_presign_secret;
    a second auto-gen call yields a different secret → 100% JWT verify failure.

    Prod guard lives in ``create_app`` where settings.env is also visible.
    """
    if not settings.allow_local_receiver:
        return None
    if settings.local_presign_secret:
        sec = str(settings.local_presign_secret)
        if len(sec) < _LOCAL_PRESIGN_SECRET_MIN_LEN:
            raise RuntimeError(
                f"BOWHEAD_LOCAL_PRESIGN_SECRET must be ≥{_LOCAL_PRESIGN_SECRET_MIN_LEN} chars (got {len(sec)}). Unset to use auto-gen.",
            )
        return sec
    import secrets

    return secrets.token_urlsafe(32)


def _resolve_artifact_fetch_secret() -> str:
    """cca P3 F-B: resolve the dedicated secret signing the artifact-fetch token the
    python_sandbox runner carries. ALWAYS non-None (unlike local_presign_secret, gated on
    allow_local_receiver) so the scoped-fetch path works for every backend + deployment.
    SEPARATE from the main JWT secret (review d3/S-1: a main-JWT token would be a full-user
    bearer). It is deterministically derived from the active JWT signing key with a fixed
    purpose label, so all gateway workers use the same scoped secret.
    """
    from bowhead_gateway.routes.auth import derive_jwt_secret_keys

    keys = derive_jwt_secret_keys("artifact-fetch")
    if not keys:
        raise RuntimeError("artifact fetch secret requires BOWHEAD_JWT_SECRET(S)")
    return keys[0][1]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_format)
    log = structlog.get_logger()
    _load_dotenv_into_process(log)
    from bowhead_core.agent_run_limits import get_runtime_controls

    get_runtime_controls()
    log.info("gateway.starting", env=settings.env)

    app.state.team_model_boundary_ready = True

    log.info(
        "team_llm.verification_flags",
        model_discovery_v2=True,
        capability_probes=True,
        azure_openai=True,
        custom_http=True,
    )

    # ─── Sprint χ Phase 0 startup gate (tech-design-r3 §0.3 / §1.1) ──
    # Verify single-worker invariant + flag consistency before any other init.
    # WEB_CONCURRENCY!=1 or BI_BATCH=1 without PARALLEL_EXEC=1 → abort.
    from bowhead_core import chi_flags as _chi_flags

    _chi_flags.assert_consistent_and_single_worker()

    # ─── Sprint α G9 startup gate (CTO P0-O-2 fix) ─────────────
    # Prod mode must NOT have dev fallback enabled — fail-loud if misconfigured.
    from bowhead_gateway.routes.auth import (
        assert_prod_auth_safe,
        assert_sandbox_backend_safe,
        assert_sandbox_executor_safe,
    )

    assert_prod_auth_safe()
    from bowhead_gateway.auth_policy import resolve_account_policy

    app.state.auth_policy = resolve_account_policy(validate_runtime=True)
    # sandbox-hardening 2026-06-17 (Phase 2a): prod must not run the no-isolation
    # subprocess sandbox backend — fail-loud at boot (mirrors the auth guard above).
    assert_sandbox_backend_safe()
    # sandbox-hardening 2026-06-17 (Phase 3 de-DooD): in executor mode, prod must have
    # the executor URL + token set, else sandbox silently fails closed (no socket).
    assert_sandbox_executor_safe()

    # ─── LLM Router ───────────────────────────────────────────
    # Platform no longer loads deployment-level providers/keys. This router serves internal assembly without team
    # context only; chat rejects unconfigured teams before sending and must not fall back to it.
    # credential-readiness D1 + batch 2-a §3.2: **default** RefusalAdapter fails loudly when actually invoked
    # (previous MockAdapter silently echoed; 9 bypasses consumed fake responses and recorded fake usage).
    # Selection **no longer checks BOWHEAD_ENV**: actual 116 config had dev, invalidating the premise
    # that production refuses. Echo requires explicit BOWHEAD_ASSEMBLY_ROUTER=mock.
    router, provider_registry = _build_assembly_router()
    loaded_cfg = None
    mode = "team-config"

    # Refusal sink → metrics (layering: router never imports core; register via usage.py hooks).
    from bowhead_router.usage import set_refusal_sink

    from bowhead_core import metrics as _metrics_mod

    # Team label (review §19 follow-up): multiple teams in one process need team attribution for /setup runtime signals;
    # refusals without team context are unattributed (shown to every team: unattributed process-level defects).
    set_refusal_sink(
        lambda ctx: _metrics_mod.inc(
            "bowhead_llm_refusal_total",
            labels={"reason": ctx.reason, "team": ctx.team_id or "unattributed"},
        )
    )

    # ─── User Memory + Session Store（SQLite） ──────────────
    db_url = settings.db_url
    # Extract file paths from URLs such as sqlite+aiosqlite:////app/.data/sqlite/bowhead.db.
    db_path = db_url.split("///", 1)[-1] if db_url.startswith("sqlite") else str(Path(".data/sqlite/bowhead.db").resolve())

    from bowhead_core.installation import preflight_installation, read_installation

    from bowhead_gateway.edition_extensions import read_build_edition

    _build_edition = read_build_edition()
    _fresh_installation = preflight_installation(db_path, _build_edition)
    if _build_edition == "community" and read_installation(db_path) is None:
        raise RuntimeError("Community installation preparation must run before Gateway startup")

    # Context-engineering sprint reduced option B: record registered flags' enablement times beside the SQLite directory
    # in append-only flag_transitions.jsonl, then partition llm_usage_daily into before/after changes.
    # Batch 2 flags took effect in 1.2.25 without a baseline; this timestamp was missing. Best effort, never blocks startup.
    try:
        from bowhead_core.context_history_flags import turn_history_enabled
        from bowhead_core.context_prefix_flags import prefix_order_enabled
        from bowhead_core.flag_transitions import record_flag_transitions

        _flag_rows = record_flag_transitions(
            {
                "BOWHEAD_CTX_PREFIX_ORDER": prefix_order_enabled(),
                "BOWHEAD_CTX_TURN_HISTORY": turn_history_enabled(),
            },
            version=_read_version(),
            data_dir=Path(db_path).parent,
        )
        if _flag_rows:
            log.info("flag_transitions.recorded", flags=_flag_rows)
    except Exception as _ft_e:  # noqa: BLE001 — Ledger failures must not block Gateway startup.
        log.warning("flag_transitions.failed", err=str(_ft_e))
    from bowhead_gateway.llm_verification_runner import (
        LlmVerificationRunner,
    )

    app.state.llm_verification_runner = LlmVerificationRunner(db_path)

    # Sprint ω Phase 0: shared-DB bootstrap (WAL + _phase0_probe).
    # MUST run before any store .initialize() so per-conn PRAGMAs land on
    # a WAL-mode file. Spike Q9 finding: 17 stores defaulted busy_timeout=0
    # against the shared DB. open_shared_ctx (per-conn) fixes this.
    from pathlib import Path as _PBoot

    _PBoot(db_path).parent.mkdir(parents=True, exist_ok=True)
    from bowhead_core._db import bootstrap_shared_db, phase0_self_test

    await bootstrap_shared_db(db_path)
    # Probe the 3 hot writers migrated in Phase 0 (turn_log / canvas / session).
    # On failure, raises → uvicorn exits non-zero → docker restart;
    # ops sees logs, runs `deploy/scripts/rollback-to.sh prev_1`.
    await phase0_self_test(db_path, ["turn_log_store", "canvas_store", "session_store"])

    memory_store = UserMemoryStore(db_path)
    session_store = SessionStore(db_path)
    canvas_store = CanvasStore(db_path)
    approval_store = ApprovalStore(db_path=db_path)
    from bowhead_core.personal_mcp import PersonalMcpStore

    await PersonalMcpStore(db_path).recover()
    # Planning Mode (M1 block 4): plan-state-machine store + boot orphan recovery (crashes require reapproval, not automatic reruns).
    from bowhead_core.plan_store import PlanStore

    plan_store = PlanStore(db_path)
    await plan_store.initialize()
    await plan_store.recover_orphans()
    # C1: retire stale PROPOSED plans unapproved after N days to prevent unbounded growth. Default 14 days.
    # CTO review F1: misconfigured nonnumeric env must not crash boot into a restart loop; use default.
    try:
        _gc_days = int(os.environ.get("BOWHEAD_PLAN_PROPOSED_GC_DAYS", "14") or 14)
    except ValueError:
        _gc_days = 14
    _gc_n = await plan_store.gc_stale_proposed(_gc_days)
    if _gc_n:
        logging.getLogger("bowhead_gateway.main").info("plan_store.gc_stale_proposed removed=%d days=%d", _gc_n, _gc_days)
    from bowhead_core.notification_store import NotificationStore

    notification_store = NotificationStore(db_path=db_path)
    turn_log_store = TurnLogStore(db_path=db_path)
    from bowhead_core.scheduled_store import ScheduledJobStore

    scheduled_store = ScheduledJobStore(db_path)
    await scheduled_store.initialize()
    # G2/G11 consolidation (docs/skills-tools-ssot.md): legacy per-user custom subagents default OFF.
    # store=None is the sole disabling point; Lead advertisements (_gather_lead_peers) and resolve's custom_store branch
    # skip via None guards; router mounting uses the same flag (include_router below).
    from bowhead_core.custom_subagent_store import (
        CustomSubagentStore,
        user_custom_subagents_enabled,
    )

    custom_subagent_store = None
    if user_custom_subagents_enabled():
        custom_subagent_store = CustomSubagentStore(db_path)
        await custom_subagent_store.initialize()
    from bowhead_core import SkillInstanceStore

    skill_instance_store = SkillInstanceStore(db_path=db_path)
    await skill_instance_store.initialize()
    # WP5-B1: a process can exit after the protected delete commits but before
    # its quarantined bundle is physically removed.  Reconcile a bounded batch
    # at boot; failures stay on the durable ledger and must never block startup.
    try:
        from bowhead_core.resource_file_quarantine import retry_pending_cleanup

        await retry_pending_cleanup(db_path, limit=200)
    except Exception as exc:  # noqa: BLE001 -- migration rollout / transient filesystem failure
        log.warning(
            "resource_file_cleanup.startup_retry_failed",
            error=type(exc).__name__,
        )

    # Artifact metadata shares SQLite; blobs use pluggable backends.
    # BOWHEAD_ARTIFACT_BACKEND=s3|local defaults local (dev needs no MinIO).

    # Sprint file-upload-presign Phase 1.2 — review P0-S-1:
    # compute ONCE and share between LocalBackend + app.state. Auto-gen path
    # would otherwise produce two different secrets across separate calls.
    _local_presign_secret = _resolve_local_presign_secret(settings)
    # cca P3 F-B: dedicated, always-present secret for the sandbox artifact-fetch token.
    _artifact_fetch_secret = _resolve_artifact_fetch_secret()
    from bowhead_core.artifact_store import build_artifact_backend_from_env

    artifact_backend = build_artifact_backend_from_env(
        db_path,
        gateway_base_url=settings.gateway_base_url,
        local_presign_secret=_local_presign_secret,
        allow_local_receiver=settings.allow_local_receiver,
    )
    artifact_store = ArtifactStore(db_path=db_path, backend=artifact_backend)

    await memory_store.initialize()
    # Permanent-storage readiness check: retain compatible reads for unclassified old databases; failure skips policy filtering.
    try:  # Memory never blocks startup.
        from bowhead_core.memory_readiness import validate_memory_storage

        await validate_memory_storage(memory_store)
    except Exception as _e:  # noqa: BLE001
        log.warning("memory_storage.readiness_failed", error=str(_e))
    await session_store.initialize()
    _chat_dispatch_owner_id = f"gateway_{secrets.token_urlsafe(18)}"
    _legacy_untracked_min_age = _legacy_untracked_orphan_min_age_minutes()
    _chat_reference_recovery_store = None
    _chat_reference_recovery_key = os.environ.get("BOWHEAD_FERNET_KEY", "")
    if _chat_reference_recovery_key:
        try:
            from bowhead_core.chat_reference_storage import (
                ChatReferenceStore,
                ReferenceCipher,
            )

            _chat_reference_recovery_store = ChatReferenceStore(
                ReferenceCipher(_chat_reference_recovery_key),
            )
        except (TypeError, ValueError):
            # Reference writer readiness already rejects an invalid key. Legacy
            # recovery stays available and accepted reference pointers remain
            # fenced until a compatible instance with the original key starts.
            log.warning("chat_reference.queue_recovery_key_invalid")

    # Reconcile tracked owners before background maintenance. In particular,
    # ``drain_pending`` protects acknowledged steer work across restart.
    try:
        _startup_recovered = await session_store.recover_expired_chat_spawn_dispatches()
        if _startup_recovered:
            log.warning(
                "session_store.chat_dispatch_startup_recovered",
                count=len(_startup_recovered),
            )
    except Exception as exc:
        log.warning(
            "session_store.chat_dispatch_startup_recovery_failed",
            error=str(exc),
        )

    async def _chat_submission_maintenance_loop() -> None:
        while True:
            # Private reference material expires even while its writer is off.
            # A cleanup failure must not suppress dispatch recovery below.
            try:
                from bowhead_core.chat_reference_retention import purge_expired_reference_material

                reference_cleanup = await purge_expired_reference_material(db_path)
                if reference_cleanup.plans or reference_cleanup.queued_inputs:
                    log.info(
                        "chat_reference.material_purged",
                        plans=reference_cleanup.plans,
                        queued_inputs=reference_cleanup.queued_inputs,
                    )
            except Exception as exc:
                log.warning("chat_reference.retention_failed", error_type=type(exc).__name__)
            # Saved helper transcripts (Continue / in-place resume) are kept for 30 days.
            try:
                from bowhead_core.subagent_resume import purge_expired as purge_expired_resume_checkpoints

                purged_checkpoints = await purge_expired_resume_checkpoints(db_path)
                if purged_checkpoints:
                    log.info("subagent_resume.checkpoints_purged", count=purged_checkpoints)
            except Exception as exc:
                log.warning("subagent_resume.retention_failed", error_type=type(exc).__name__)
            try:
                recovered = await session_store.recover_expired_chat_spawn_dispatches(
                    reference_store=_chat_reference_recovery_store,
                )
                if recovered:
                    log.warning(
                        "session_store.chat_dispatch_owner_lost",
                        count=len(recovered),
                    )
                if _legacy_untracked_min_age is not None:
                    stale = await session_store.cleanup_stale_running_turns(
                        older_than_min=_legacy_untracked_min_age,
                    )
                    if stale:
                        log.warning(
                            "session_store.chat_untracked_orphans_terminalized",
                            count=stale,
                            min_age_minutes=_legacy_untracked_min_age,
                        )
                        recovered_after_cleanup = await session_store.recover_expired_chat_spawn_dispatches(
                            reference_store=_chat_reference_recovery_store,
                        )
                        if recovered_after_cleanup:
                            log.warning(
                                "session_store.chat_orphan_buffers_recovered",
                                count=len(recovered_after_cleanup),
                            )
                await plan_store.recover_orphans()
                await session_store.cleanup_chat_submission_protocol()
                await session_store.cleanup_composer_telemetry_dedupe()
            except Exception as exc:
                log.warning(
                    "session_store.chat_submission_maintenance_failed",
                    error=str(exc),
                )
            await asyncio.sleep(15)

    # Terminal Protocol v1 readers/lifecycle remain active independently of
    # writer admission.  A deployment without migration 0008 keeps legacy
    # lifecycle only; S7 readiness will refuse the v1 writer explicitly.
    from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
    from bowhead_core.terminal_protocol_store import TerminalProtocolStore

    terminal_protocol_store = TerminalProtocolStore(db_path)
    app.state.legacy_session_lifecycle_allowed = False
    try:
        await terminal_protocol_store.initialize()
    except RuntimeError as exc:
        if await session_store.terminal_protocol_schema_mode() != "legacy":
            raise
        app.state.terminal_protocol_store = None
        app.state.session_lifecycle_coordinator = None
        app.state.legacy_session_lifecycle_allowed = True
        log.warning("terminal_protocol.schema_not_ready", error=str(exc))
    else:
        app.state.terminal_protocol_store = terminal_protocol_store
        app.state.session_lifecycle_coordinator = SessionLifecycleCoordinator(
            session_store,
            terminal_protocol_store,
            canvas_store,
        )
    # Untracked rows can only come from a pre-D0b writer. Their per-Turn age
    # fence is at least twice the effective Lead hard timeout (and never below
    # 60 minutes), so a long rolling deployment cannot make a fresh old-writer
    # Turn eligible. Keep that timeout frozen for the mixed-version window.
    _chat_submission_maintenance_task = asyncio.create_task(
        _chat_submission_maintenance_loop(),
        name="chat-submission-maintenance",
    )
    try:
        _expired_submissions = await session_store.cleanup_chat_submission_protocol()
        if _expired_submissions > 0:
            log.info(
                "session_store.expired_chat_submissions_cleaned",
                count=_expired_submissions,
            )
    except Exception as _e:
        # Migration rollout may temporarily start a compatible image before the
        # core schema reaches the A head. Readiness gates the feature itself;
        # cleanup must not make the otherwise compatible gateway unavailable.
        log.warning("session_store.chat_submission_cleanup_failed", error=str(_e))
    try:
        await session_store.cleanup_composer_telemetry_dedupe()
    except Exception as _e:
        log.warning("session_store.composer_telemetry_cleanup_failed", error=str(_e))
    await canvas_store.initialize()
    # Canvas export reliability W1-E: immutable ExportSnapshot schema/store.
    # Candidate serialization happens outside writer locks; W1-F adds asset
    # metadata pins to the same validate-and-freeze transaction.
    from bowhead_core.export_snapshot import ExportSnapshotStore

    export_snapshot_store = ExportSnapshotStore(db_path)
    await export_snapshot_store.initialize()
    # WP1 export flow: export-job table + boot recovery (interrupted jobs fail, historical exports remain downloadable).
    from bowhead_core.canvas_export import init_export_jobs

    await init_export_jobs(db_path)
    from bowhead_core.export_store import ExportStore

    export_store = ExportStore(db_path, export_snapshot_store)
    await export_store.initialize()
    from bowhead_core.export_runner import (
        DurableLocalExportRunner,
        ExportProcessorRegistry,
    )

    export_processor_registry = ExportProcessorRegistry()
    from bowhead_core.export_pdf_processor import (
        CanvasPdfExportProcessor,
        build_playwright_renderer_factory,
    )

    frontend_dir = Path(
        os.environ.get(
            "BOWHEAD_EXPORT_FRONTEND_DIR",
            str(Path(__file__).resolve().parents[3] / "bowhead_frontend"),
        )
    ).resolve()
    node_value = os.environ.get("BOWHEAD_EXPORT_NODE_PATH") or shutil.which("node")
    pdftoppm_value = os.environ.get("BOWHEAD_EXPORT_PDFTOPPM_PATH") or shutil.which("pdftoppm")
    script_path = frontend_dir / "scripts" / "render-export-visual.mjs"
    if (
        node_value is not None
        and pdftoppm_value is not None
        and Path(node_value).is_file()
        and Path(pdftoppm_value).is_file()
        and frontend_dir.is_dir()
        and script_path.is_file()
    ):
        pdf_processor = CanvasPdfExportProcessor(
            db_path=db_path,
            snapshot_store=export_snapshot_store,
            artifact_store=artifact_store,
            staging_root=Path(
                os.environ.get(
                    "BOWHEAD_EXPORT_STAGING_ROOT",
                    "/tmp/bowhead-exports",
                )
            ).resolve(),
            pdftoppm_path=Path(pdftoppm_value).resolve(),
            browser_renderer_factory=build_playwright_renderer_factory(
                frontend_dir=frontend_dir,
                frontend_base_url=os.environ.get(
                    "BOWHEAD_EXPORT_FRONTEND_BASE_URL",
                    "http://127.0.0.1:8080",
                ),
                node_binary=Path(node_value).resolve(),
                script_path=script_path,
            ),
        )
        export_processor_registry.register(
            fmt="pdf",
            profile="full_report",
            handler=pdf_processor.process,
        )
        log.info(
            "canvas_export.pdf_v2_processor_registered",
            frontend_base_url=os.environ.get(
                "BOWHEAD_EXPORT_FRONTEND_BASE_URL",
                "http://127.0.0.1:8080",
            ),
        )
    else:
        log.error(
            "canvas_export.pdf_v2_processor_unavailable",
            node_ready=bool(node_value and Path(node_value).is_file()),
            poppler_ready=bool(pdftoppm_value and Path(pdftoppm_value).is_file()),
            frontend_ready=frontend_dir.is_dir(),
            script_ready=script_path.is_file(),
        )
    from bowhead_core.export_pptx_processor import (
        CanvasPptxExportProcessor,
        build_playwright_renderer_factory,
    )

    frontend_dir = Path(
        os.environ.get(
            "BOWHEAD_EXPORT_FRONTEND_DIR",
            str(Path(__file__).resolve().parents[3] / "bowhead_frontend"),
        )
    ).resolve()
    node_value = os.environ.get("BOWHEAD_EXPORT_NODE_PATH") or shutil.which("node")
    pdftoppm_value = os.environ.get("BOWHEAD_EXPORT_PDFTOPPM_PATH") or shutil.which("pdftoppm")
    soffice_value = os.environ.get("BOWHEAD_EXPORT_SOFFICE_PATH") or shutil.which("soffice")
    script_path = frontend_dir / "scripts" / "render-export-visual.mjs"
    if (
        node_value is not None
        and pdftoppm_value is not None
        and soffice_value is not None
        and Path(node_value).is_file()
        and Path(pdftoppm_value).is_file()
        and Path(soffice_value).is_file()
        and frontend_dir.is_dir()
        and script_path.is_file()
    ):
        pptx_processor = CanvasPptxExportProcessor(
            db_path=db_path,
            snapshot_store=export_snapshot_store,
            artifact_store=artifact_store,
            staging_root=Path(
                os.environ.get(
                    "BOWHEAD_EXPORT_STAGING_ROOT",
                    "/tmp/bowhead-exports",
                )
            ).resolve(),
            soffice_path=Path(soffice_value).resolve(),
            pdftoppm_path=Path(pdftoppm_value).resolve(),
            browser_renderer_factory=build_playwright_renderer_factory(
                frontend_dir=frontend_dir,
                frontend_base_url=os.environ.get(
                    "BOWHEAD_EXPORT_FRONTEND_BASE_URL",
                    "http://127.0.0.1:8080",
                ),
                node_binary=Path(node_value).resolve(),
                script_path=script_path,
            ),
        )
        for profile in (
            "full_report",
            "executive_summary",
        ):
            export_processor_registry.register(
                fmt="pptx",
                profile=profile,
                handler=pptx_processor.process,
            )
        log.info(
            "canvas_export.pptx_v2_processor_registered",
            frontend_base_url=os.environ.get(
                "BOWHEAD_EXPORT_FRONTEND_BASE_URL",
                "http://127.0.0.1:8080",
            ),
        )
    else:
        log.error(
            "canvas_export.pptx_v2_processor_unavailable",
            node_ready=bool(node_value and Path(node_value).is_file()),
            poppler_ready=bool(pdftoppm_value and Path(pdftoppm_value).is_file()),
            libreoffice_ready=bool(soffice_value and Path(soffice_value).is_file()),
            frontend_ready=frontend_dir.is_dir(),
            script_ready=script_path.is_file(),
        )
    from bowhead_core.export_xlsx_processor import (
        CanvasXlsxExportProcessor,
        build_playwright_renderer_factory,
    )

    frontend_dir = Path(
        os.environ.get(
            "BOWHEAD_EXPORT_FRONTEND_DIR",
            str(Path(__file__).resolve().parents[3] / "bowhead_frontend"),
        )
    ).resolve()
    node_value = os.environ.get("BOWHEAD_EXPORT_NODE_PATH") or shutil.which("node")
    soffice_value = os.environ.get("BOWHEAD_EXPORT_SOFFICE_PATH") or shutil.which("soffice")
    script_path = frontend_dir / "scripts" / "render-export-visual.mjs"
    if (
        node_value is not None
        and soffice_value is not None
        and Path(node_value).is_file()
        and Path(soffice_value).is_file()
        and frontend_dir.is_dir()
        and script_path.is_file()
    ):
        xlsx_processor = CanvasXlsxExportProcessor(
            db_path=db_path,
            snapshot_store=export_snapshot_store,
            artifact_store=artifact_store,
            staging_root=Path(
                os.environ.get(
                    "BOWHEAD_EXPORT_STAGING_ROOT",
                    "/tmp/bowhead-exports",
                )
            ).resolve(),
            soffice_path=Path(soffice_value).resolve(),
            browser_renderer_factory=(
                build_playwright_renderer_factory(
                    frontend_dir=frontend_dir,
                    frontend_base_url=os.environ.get(
                        "BOWHEAD_EXPORT_FRONTEND_BASE_URL",
                        "http://127.0.0.1:8080",
                    ),
                    node_binary=Path(node_value).resolve(),
                    script_path=script_path,
                )
            ),
        )
        export_processor_registry.register(
            fmt="xlsx",
            profile="data_workbook",
            handler=xlsx_processor.process,
        )
        log.info(
            "canvas_export.xlsx_v2_processor_registered",
            frontend_base_url=os.environ.get(
                "BOWHEAD_EXPORT_FRONTEND_BASE_URL",
                "http://127.0.0.1:8080",
            ),
        )
    else:
        log.error(
            "canvas_export.xlsx_v2_processor_unavailable",
            node_ready=bool(node_value and Path(node_value).is_file()),
            libreoffice_ready=bool(soffice_value and Path(soffice_value).is_file()),
            frontend_ready=frontend_dir.is_dir(),
            script_ready=script_path.is_file(),
        )
    export_runner = DurableLocalExportRunner(
        store=export_store,
        processor=export_processor_registry,
        renderer_contract_versions=(1, 2),
    )
    await approval_store.initialize()
    await notification_store.initialize()
    await turn_log_store.initialize()
    await artifact_store.initialize()
    # Sprint ν1: reference audit-log table + WAL pragma (same db_path).
    from bowhead_core._ref_audit import init_ref_audit

    await init_ref_audit(db_path)

    # Automatically persist events from all TurnEventBus instances. Workers upgrade their buses to
    # persist-before-live; safe pointers also explicitly await preceding same-turn events and their own persistence before
    # entering the live ring, preserving live/persist/replay order and preventing recovery from creating
    # live-only tail frames.
    ordered_turn_log_sink = OrderedTurnLogSink(turn_log_store)
    get_event_registry().set_log_sink(ordered_turn_log_sink)
    app.state.ordered_turn_log_sink = ordered_turn_log_sink

    # fix/sse-fanout-bus: announce delivery mode in one boot log line; rollback validation greps this line.
    from bowhead_core.events import _fanout_enabled

    log.info("sse_fanout", enabled=_fanout_enabled(), flag="BOWHEAD_SSE_FANOUT")

    # ─── Skill system（loader + catalog + sandbox runtime） ──
    skill_catalog, skill_runtime = build_default_skill_stack()
    from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
    from bowhead_core.uploaded_skill_reference_metadata import UploadedSkillMetadataCache

    builtin_skill_reference_metadata = await anyio.to_thread.run_sync(build_builtin_skill_metadata)
    uploaded_skill_reference_metadata = UploadedSkillMetadataCache(db_path)

    # ─── V2.1.5 / V3 PRD-B: user-settings store (i18n / dreaming pause / etc.) ──
    # Initialize early: PauseStateCache needs settings_store; DreamingLight needs PauseStateCache.
    from bowhead_core.user_settings_store import UserSettingsStore

    user_settings_store = UserSettingsStore(db_path)
    await user_settings_store.init()

    from bowhead_core.personal_appearance_service import PersonalAppearanceService
    from bowhead_core.personal_appearance_store import PersonalAppearanceStore

    personal_appearance_store = PersonalAppearanceStore(user_settings_store.db_path)
    await personal_appearance_store.initialize()
    personal_appearance_service = PersonalAppearanceService(personal_appearance_store)

    from bowhead_core.home_proposal_cache_store import HomeProposalCacheStore

    home_proposal_cache_store = HomeProposalCacheStore(db_path)
    await home_proposal_cache_store.initialize()

    # ─── Theme config M1: team-level theme policy store ─────────────────────
    from bowhead_core.team_settings_store import TeamSettingsStore

    team_settings_store = TeamSettingsStore(db_path)
    await team_settings_store.init()

    from bowhead_core.chart_style_read_service import ChartStyleReadService
    from bowhead_core.chart_style_store import ChartStyleStore
    from bowhead_core.chart_style_uow import ChartStyleUnitOfWork

    chart_style_store = ChartStyleStore(db_path)
    chart_style_read_service = ChartStyleReadService(db_path)
    chart_style_uow = ChartStyleUnitOfWork(db_path)

    # ─── V3 PRD-B: metrics event log + pause state cache ─────
    from bowhead_core.metrics_log import MetricsLog
    from bowhead_memory.candidates import PauseStateCache

    metrics_log = MetricsLog(db_path)
    await metrics_log.initialize()
    pause_state_cache = PauseStateCache(settings_store=user_settings_store)

    # ─── V3.5 PRD-C: MemoryNotesStore ────────────────────────
    from bowhead_core.memory_notes import MemoryNotesStore

    memory_notes_store = MemoryNotesStore(db_path)
    await memory_notes_store.initialize()

    # ─── V4 PRD-D-4-4: MemoryPackSnapshotsStore (replay audit log) ─
    from bowhead_core.memory_pack_snapshots import (
        MemoryPackSnapshotsStore,
    )

    memory_pack_snapshots_store = MemoryPackSnapshotsStore(db_path)
    await memory_pack_snapshots_store.initialize()

    # Space instructions: tables are owned by migration 0178, so there is no initialize().
    from bowhead_core.space_instructions import SpaceInstructionsStore

    space_instructions_store = SpaceInstructionsStore(db_path)

    # ─── V4 Sprint 2 PRD-D-4-1: MemoryNoteAnchorsStore (per-bullet anchors) ─
    from bowhead_core.memory_note_anchors import (
        MemoryNoteAnchorsStore,
    )

    memory_note_anchors_store = MemoryNoteAnchorsStore(db_path)
    await memory_note_anchors_store.initialize()

    # Memory redesign batch 0④: persistence gate for turn-start memory snapshots. Previously two envs, BOWHEAD_REPLAY_ENABLED +
    # BOWHEAD_REPLAY_PCT (defaults 0/0, both absent in production, permanently leaving replay with zero rows);
    # batch 0③ added entry ids. Unsynchronized gates only create disagreement between replayed and actually injected memories.
    from bowhead_core.memory_readiness import memory_storage_ready

    memory_pack_snapshots_enabled = memory_storage_ready()
    # V4 PRD-D-5: separate enable for memory↔canvas writes (independent kill switch).
    memory_from_canvas_enabled = os.environ.get("BOWHEAD_MEMORY_FROM_CANVAS_ENABLED", "0") in ("1", "true")
    # V4 Sprint 2 PRD-D-4-1: cascade invalidation hook + per-bullet UI gate.
    anchor_invalidation_enabled = os.environ.get("BOWHEAD_ANCHOR_INVALIDATION_ENABLED", "0") in ("1", "true")
    # V5 PRD-E2: inline tool result preview emit gate.
    # Sprint chat-v3 Phase A+B (2026-05-13): default flipped 0 → 1.
    # Reason: Phase A `tool_finding` requires preview_payload to be non-None
    # to schedule the micro LLM summary. Keeping default OFF would mean the
    # entire v3 finding feature is dark in prod (Phase A review P0-A1).
    # Rollback path: set BOWHEAD_INLINE_PREVIEW_ENABLED=0 in env / compose to
    # revert to pre-0.16.0 behavior (no inline preview, no finding emit).
    inline_preview_enabled = os.environ.get("BOWHEAD_INLINE_PREVIEW_ENABLED", "1") in ("1", "true")
    nl_intent_enabled = os.environ.get("BOWHEAD_NL_INTENT_ENABLED", "0") in ("1", "true")
    # memory-revive S1: turn-finalization Dreaming Light distillation (one LLM call per turn).
    # **Default ON** (user decision 2026-07-22: small user base, no rollout); =0 remains a kill switch.
    # Enabled = orch receives dreaming_light (mirrors NL intent classifier gating; OFF passes None,
    # so turn_runner trigger creates no task). DreamingLight cost guards: per-user
    # 60s cooldown + daily cap 40. Pause routes' app.state.dreaming_light always exists, unaffected by this flag.
    dreaming_enabled = os.environ.get("BOWHEAD_DREAMING_ENABLED", "1") in ("1", "true")

    # Privacy ledger and memory-operation fingerprints share the instance's
    # Fernet root key, so private deployments require no second ledger secret.
    _privacy_ledger_key = os.environ.get("BOWHEAD_FERNET_KEY", "").strip()

    # ─── Memory candidates + Dreaming Light ──────────────────
    memory_candidate_store = MemoryCandidateStore(
        db_path=db_path,
        fingerprint_key=(_privacy_ledger_key or _artifact_fetch_secret),
    )
    await memory_candidate_store.initialize()
    personal_memory_governance_store = PersonalMemoryGovernanceStore(
        db_path,
        root_secret=_artifact_fetch_secret,
    )
    dreaming_light = DreamingLight(
        router=router,
        candidate_store=memory_candidate_store,
        pause_cache=pause_state_cache,
    )

    # Memory V2 privacy boundary. The operation schema is always additive;
    # permanent-delete capability remains closed until the external ledger and
    # secure-delete baseline are both ready.
    from bowhead_core.memory_privacy import (
        MemoryDestructiveOperationStore,
        PrivacyDeletionLedger,
    )

    _privacy_ledger_path = os.environ.get(
        "BOWHEAD_PRIVACY_LEDGER_DB",
        "/app/.privacy-ledger/deletions.db",
    ).strip()
    privacy_deletion_ledger = (
        PrivacyDeletionLedger(
            _privacy_ledger_path,
            hmac_key=_privacy_ledger_key,
            key_version=os.environ.get(
                "BOWHEAD_PRIVACY_LEDGER_KEY_VERSION",
                "v1",
            ),
        )
        if _privacy_ledger_path and _privacy_ledger_key
        else None
    )
    if privacy_deletion_ledger is not None:
        await privacy_deletion_ledger.initialize()
    memory_destructive_operation_store = MemoryDestructiveOperationStore(
        db_path,
        ledger=privacy_deletion_ledger,
        fingerprint_key=(_privacy_ledger_key or _artifact_fetch_secret),
    )
    await memory_destructive_operation_store.initialize()
    if privacy_deletion_ledger is not None:
        replay_result = await memory_destructive_operation_store.replay_committed_deletions()
        log.info(
            "memory.privacy_ledger_replay_complete",
            **replay_result,
        )
    recovery_result = await memory_destructive_operation_store.resume_incomplete_operations()
    if recovery_result["operations_seen"]:
        log.info(
            "memory.destructive_operation_recovery_complete",
            **recovery_result,
        )

    # ─── M156: Workspace schema cache (TTL prevents list_measures/dims bursts) ──
    from bowhead_core.datarc_schema_cache import DatarcSchemaCache

    schema_cache = DatarcSchemaCache(ttl_sec=300, max_entries=64)

    # ─── M156-M3b: Clarification HITL store + background expiration task ───
    from bowhead_core.clarification_store import ClarificationStore

    clarification_store = ClarificationStore(db_path=db_path)
    await clarification_store.initialize()
    # Move migration gates earlier: unmigrated databases (or only 1.0.0 baseline) must reject startup before any recovery/maintenance
    # jobs touch new tables, asking for migration first instead of background no-such-table failures (e.g. KB maintenance kb_rebuild_jobs).
    # Stores participating in atomic invalidation must share one SQLite file, with core 0017 installed.
    from bowhead_gateway.clarification_invalidation import require_shared_core_database

    require_shared_core_database(
        clarification_store,
        scheduled_store,
        turn_log_store,
        session_store,
    )
    await clarification_store.require_invalidation_schema()
    _clarification_invalidation_publisher_task: asyncio.Task[None] | None = None

    _terminal_recovery_monitor_task: asyncio.Task[Any] | None = None
    _terminal_writer_service: Any | None = None
    _terminal_recovery_tasks: set[asyncio.Task[Any]] = set()
    _terminal_recovery_triggers_enabled = False
    _terminal_auth_revoke_task: asyncio.Task[Any] | None = None

    # P0 #5: periodically expire pending items in background to prevent monotonic DB growth.
    _clarify_expire_task: asyncio.Task[None] | None = None

    async def _awaiting_clarify_turns() -> set[str]:
        """挂起中的定时任务 turn——这些 turn 的 clarification 豁免 24h 清扫。

        为什么（scheduled-recipe-runs S3 · FR-5.4）: 挂起卡的生命周期归 scheduled 侧管
        （活到任务自身下一个周期）, 被这条系统清扫提前杀掉会让周级挂起「深链进来无卡可答」,
        且过期判据会把 expired 误读成「已回答」造成永久挂起。
        best-effort: 取不到 store 或查询抛错一律降级为「不排除」——清扫本身防的是 db 单调
        增长, 绝不能因为拿不到豁免名单就整个停摆。
        """
        _sched = getattr(app.state, "scheduled_store", None)
        if _sched is None:
            return set()
        try:
            return set(await _sched.awaiting_turn_ids())
        except Exception as e:
            log.warning("clarification.awaiting_exclude_unavailable", error=str(e))
            return set()

    async def _clarify_expire_loop() -> None:
        while True:
            try:
                await asyncio.sleep(3600)  # Hourly.
                from bowhead_gateway.terminal_control_coordinator import (
                    expire_due_terminal_decisions,
                )

                terminal_expired = await expire_due_terminal_decisions(
                    app,
                    ttl_hours=24.0,
                )
                # exclude_turn_ids (scheduled-recipe-runs S3): suspended scheduled-task cards must survive this shared 24h sweep;
                # scheduled owns their lifecycle (until the job's next period).
                n = await clarification_store.expire_old(
                    ttl_hours=24.0,
                    exclude_turn_ids=await _awaiting_clarify_turns(),
                )
                if n:
                    from bowhead_gateway.clarification_invalidation import (
                        publish_pending_clarification_invalidations,
                    )

                    await publish_pending_clarification_invalidations(app)
                invalidation_outbox_cleaned = await clarification_store.cleanup_delivered_invalidations(
                    retention_days=30,
                    limit=5000,
                )
                if n or terminal_expired or invalidation_outbox_cleaned:
                    log.info(
                        "clarification.expired_old",
                        legacy_count=n,
                        terminal_count=terminal_expired,
                        invalidation_outbox_cleaned=invalidation_outbox_cleaned,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("clarification.expire_loop_failed", error=str(e))

    _clarify_expire_task = asyncio.create_task(_clarify_expire_loop())

    # #54 long-term fix (2026-04-29): nightly artifact GC hard-deletes items soft-deleted >30 days, preventing DB growth +
    # stale artifact_ids causing LLM references to 404. Every 24h, first run 60s after startup.
    _artifact_gc_task: asyncio.Task[None] | None = None

    async def _artifact_gc_loop() -> None:
        await asyncio.sleep(60)
        while True:
            try:
                if artifact_store is not None:
                    # artifact-volume batch 0: reconcile missing originals (flag OFF only counts). Summarize every cycle;
                    # unhealthy storage/proportion-gate hits log ERROR, exposing backend changes/unmounted volumes that day.
                    try:
                        from bowhead_core.artifact_reconcile import (
                            reconcile_apply_enabled,
                            reconcile_missing_artifacts,
                        )

                        _rec = await reconcile_missing_artifacts(artifact_store, apply=reconcile_apply_enabled())
                        if _rec.skipped_reason in ("storage_unhealthy", "ratio_gate"):
                            log.error("artifact_reconcile.blocked", **_rec.summary(), sample=_rec.sample_absent[:5])
                        else:
                            log.info("artifact_reconcile.done", **_rec.summary())
                    except Exception as _e:  # noqa: BLE001 - Reconciliation failures do not affect other GC.
                        log.warning("artifact_reconcile.failed", error=str(_e))
                    # file-management-v2 P2: **reconcile before hard deletion**. KB-referenced artifacts should not remain
                    # in trash awaiting GC; hard deletion leaves retrieval matches but permanently unavailable originals (404),
                    # discovery §3.6's silent partial failure, irreparable beyond the 30-day window.
                    # D2's 409 guard blocks only new references; this rescues existing data **predating** that guard,
                    # also serving as a long-term fallback for future write-path bypasses.
                    _kb_store = getattr(app.state, "kb_files_store", None)
                    if _kb_store is not None:
                        try:
                            # Exhaust all pages: list_soft_deleted_ids has a batch cap, while
                            # hard_delete_old_soft_deleted does not. Reconciling only the first batch
                            # silently hard-deletes KB-referenced files beyond it in large trash collections.
                            _trashed: list[str] = []
                            _batch_size = 5000
                            _seen = 0
                            while True:
                                _batch = await artifact_store.list_soft_deleted_ids(
                                    limit=_batch_size,
                                    offset=_seen,
                                )
                                if not _batch:
                                    break
                                _trashed.extend(_batch)
                                _seen += len(_batch)
                                if len(_batch) < _batch_size:
                                    break
                            _still_ref = await _kb_store.artifact_ids_in_kb(_trashed) if _trashed else set()
                            if _still_ref:
                                _n = await artifact_store.force_restore(sorted(_still_ref))
                                log.warning(
                                    "artifact_gc.kb_reference_reconciled",
                                    restored=_n,
                                    ids=sorted(_still_ref)[:20],
                                )
                        except Exception as _e:
                            # Reconciliation failure must **block this cycle's hard deletion**; prefer another day in trash
                            # over irreversible deletion without knowing which files are referenced.
                            log.error("artifact_gc.kb_reconcile_failed_skipping_gc", error=str(_e))
                            await asyncio.sleep(3600)
                            continue
                    # file-management-v2 P4: runtime-data TTL. Agent byproducts grow monotonically without
                    # user awareness, filling disk after months without cleanup. Soft policy:
                    # only soft-delete to trash (recoverable, then 30-day hard deletion below), skipping
                    # pinned/canvas-linked artifacts. Assets (uploads/finished outputs/exports) never expire automatically.
                    #
                    # ⚠ Recheck candidates against kb_files: KB-upload originals use routes/kb.py
                    # put_bytes **without kind**, receiving default
                    # tool_data; kind alone classifies them as runtime data. Without this check, TTL
                    # trashes every KB document's original bytes, hard-deleting after 30 days.
                    # kb_files is in another database, preventing joins; merge here instead.
                    try:
                        _ttl_days = int(os.getenv("BOWHEAD_RUN_DATA_TTL_DAYS", "30"))
                    except ValueError:
                        _ttl_days = 30
                    if _ttl_days > 0:
                        try:
                            _cands = await artifact_store.stale_run_data_ids(days=_ttl_days)
                            _protected: set[str] = set()
                            if _cands and _kb_store is not None:
                                _protected = set(await _kb_store.artifact_ids_in_kb(_cands))
                            _expired = await artifact_store.expire_run_data_ids([i for i in _cands if i not in _protected])
                            log.info(
                                "artifact_gc.run_data_expired",
                                count=_expired,
                                kb_protected=len(_protected),
                                days=_ttl_days,
                            )
                        except Exception as _e:  # noqa: BLE001 - TTL failures must not break GC.
                            log.warning("artifact_gc.run_data_ttl_failed", error=str(_e))
                    stats = await artifact_store.hard_delete_old_soft_deleted(days=30)
                    log.info("artifact_gc.done", **stats)
                await asyncio.sleep(86400)  # 24h
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("artifact_gc.failed", error=str(e))
                await asyncio.sleep(3600)  # Retry 1h after errors.

    if artifact_store is not None:
        _artifact_gc_task = asyncio.create_task(_artifact_gc_loop())

    # V3 PRD-B memory GC task is registered later — needs user_store dep.
    _memory_gc_task: asyncio.Task[None] | None = None

    # ─── VectorClient（Datarc Embedding Service） ────────────
    # M2.6a: create a global client during lifespan, inject into Orchestrator + app.state.
    # Startup checks health and **actual existence of the workspace's collections**
    # (v1_<tenant>_p<workspace> + _keywords), avoiding first-call surprises from wrong
    # collection names or missing Datarc indexes.
    vector_client: VectorClient | None = None
    # Empty URL = unconfigured global direct vector connection (settings default; old hostname expired); skip probes,
    # equivalent to disabled. Per-turn embeddings via team Datarc proxy remain unaffected.
    if settings.vector_db_enabled and settings.vector_db_url:
        vc = VectorClient(
            base_url=settings.vector_db_url,
            access_token=settings.vector_db_token or None,
            timeout_sec=settings.vector_db_timeout_sec,
        )
        try:
            h = await vc.health()
            log.info(
                "vector_db.ready",
                url=settings.vector_db_url,
                tenant=settings.vector_db_tenant,
                health=h if isinstance(h, dict) else "(opaque)",
            )
            vector_client = vc
        except VectorDBError as e:
            log.warning("vector_db.health_failed_disabled", url=settings.vector_db_url, status=e.status, body=e.body)
            await vc.aclose()
        except Exception as e:
            log.warning("vector_db.health_unexpected_error_disabled", url=settings.vector_db_url, error=str(e))
            await vc.aclose()
    elif settings.vector_db_enabled:
        # Enabled with empty URL (settings default): distinguish from explicitly disabled for operations diagnosis.
        log.info("vector_db.no_url_configured")
    else:
        log.info("vector_db.disabled_by_settings")

    # ─── Section Router (canvas mode) ─────────────────────────

    # ─── Tool registry (bi.* + skill.* + subagent_spawn) ─────
    tool_registry = build_default_registry()
    register_skill_tools(tool_registry, skill_catalog, skill_runtime)
    from bowhead_core.deck_render_tool import register_deck_render_tool

    register_deck_render_tool(tool_registry, skill_runtime)
    from bowhead_core.subagent import register_subagent_tool

    register_subagent_tool(tool_registry)
    # M137 #142: subagent_return, contract tool for subagents' structured completion payloads.
    # Lead is excluded from the allowlist; only compute/specialist profiles see it.
    from bowhead_core.subagent_return_tool import register_subagent_return_tool

    register_subagent_return_tool(tool_registry)
    from bowhead_core.skill_save_tool import register_skill_save_tool

    register_skill_save_tool(tool_registry)
    from bowhead_core.scheduled_job_tool import register_scheduled_job_tool

    register_scheduled_job_tool(tool_registry)  # flag-gated BOWHEAD_SCHEDULED_JOB_TOOL (default OFF)
    from bowhead_core.canvas_tools import register_canvas_tools

    register_canvas_tools(tool_registry)
    from bowhead_core.agents.clarification_tool import register_clarification_tool

    register_clarification_tool(tool_registry)
    from bowhead_core.agents.plan_propose_tool import register_plan_propose_tool

    register_plan_propose_tool(tool_registry)
    # remember-rebuild (dogfood s_3e854 item 2): remember / recall Lead tools.
    from bowhead_core.memory_tools import register_memory_tools

    register_memory_tools(tool_registry)
    # bi-hitl-cleanup B1 (sprint 2026-05-21): BI concept resolve atomic tool.
    from bowhead_core.agents.bi_concept_resolve import (
        register_bi_concept_resolve_tool,
    )

    register_bi_concept_resolve_tool(tool_registry)
    # web_search: external internet search resolves only team search configuration (no deployment/env keys).
    # No team resolver means no registration; teams without active models get an unconfigured result. Register **before**
    # role-binding pass so YAML→codegen allowed_for_roles (lead/analyst) takes effect.
    from bowhead_core.web_search import register_web_search_tool
    from bowhead_core.web_tavily_tools import TavilyCredential, register_web_tavily_tools

    async def _team_tavily_credential(team_id: str) -> TavilyCredential | None:
        """团队已启用 Tavily 且有密钥时返回凭据；web_search 与 Tavily 网页工具共用，每次调用现读。"""
        store = getattr(app.state, "user_store", None)
        if not team_id or store is None:
            return None
        tavily = await store.get_team_builtin_tool(team_id, "tavily")
        if tavily is None or tavily.status != "enabled":
            return None
        auth = await store.get_team_builtin_tool_auth_decrypted(team_id, "tavily")
        key = str(auth.get("api_key") or "")
        return TavilyCredential(api_key=key, source_id=tavily.id) if key else None

    async def _team_search_resolver(team_id: str):  # type: ignore[no-untyped-def]
        if not team_id:
            return None
        store = getattr(app.state, "user_store", None)
        if store is None:
            return None
        try:
            tavily_cred = await _team_tavily_credential(team_id)
            if tavily_cred is not None:
                from bowhead_core.search_config import SearchModelRegistry, SearchModelSpec

                spec = SearchModelSpec(
                    name="tavily",
                    capability="web_search",
                    provider="tavily",
                    real_id="tavily",
                    base_url="https://api.tavily.com",
                    api_key=tavily_cred.api_key,
                    timeout_sec=20,
                    protocol="tavily",
                )
                return SearchModelRegistry(by_name={"tavily": spec}, active={"web_search": "tavily"})
            cfg, _ver = await store.get_team_llm_config_with_version(team_id)
            if not cfg:
                return None
            from bowhead_core.search_config import build_team_search_registry
            from bowhead_core.team_llm_config import migrate_legacy_config_to_v2

            cfg = migrate_legacy_config_to_v2(cfg)
            if not cfg.get("search"):
                return None
            return build_team_search_registry(cfg)
        except Exception:
            logging.getLogger("bowhead_gateway.main").warning("web_search.team_search_resolve_failed team=%s", team_id, exc_info=True)
            return None

    register_web_search_tool(tool_registry, team_search_resolver=_team_search_resolver)
    # web_extract / web_map: native Tavily web tools, unregistered when FEATURE_TAVILY_TOOLS is OFF.
    register_web_tavily_tools(tool_registry, _team_tavily_credential, store_getter=lambda: getattr(app.state, "user_store", None))
    # web_fetch: retrieve known URLs' bodies (user links / full web_search results). Always registered,
    # soft-disabled via BOWHEAD_WEB_FETCH=0. Reuse ssrf_guard for SSRF; credentials use Tier-1 headers.
    from bowhead_core.web_fetch import register_web_fetch_tool

    register_web_fetch_tool(tool_registry)
    # Image generation resolves only team configuration; unconfigured tools return an explicit unconfigured result.
    from bowhead_core.media.config_loader import empty_media_registry
    from bowhead_core.media.image_gen_tool import register_image_gen_tool

    async def _team_media_resolver(team_id: str):  # type: ignore[no-untyped-def]
        if not team_id:
            return None
        store = getattr(app.state, "user_store", None)
        if store is None:
            return None
        try:
            cfg, _ver = await store.get_team_llm_config_with_version(team_id)
            if not cfg:
                return None
            from bowhead_core.media.config_loader import build_team_media_registry
            from bowhead_core.team_llm_config import migrate_legacy_config_to_v2

            cfg = migrate_legacy_config_to_v2(cfg)
            if not cfg.get("media"):
                return None
            return build_team_media_registry(cfg)
        except Exception:
            logging.getLogger("bowhead_gateway.main").warning("image_gen.team_media_resolve_failed team=%s", team_id, exc_info=True)
            return None

    register_image_gen_tool(tool_registry, empty_media_registry(), team_media_resolver=_team_media_resolver)

    from bowhead_core.documents.tool import register_document_tool

    register_document_tool(tool_registry, service=lambda: app.state.document_service)

    # M158 paradigm v2: apply role bindings to all registered tools once.
    # paradigm_v2_enabled=False workspaces have ctx.role=None, skipping enforcement and retaining behavior.
    from bowhead_core.tool_registry import apply_paradigm_v2_role_bindings

    apply_paradigm_v2_role_bindings(tool_registry)

    # cca P2.1 (C1): the tool_creator TRIGGER — flag-gated (BOWHEAD_CCA_TOOL_CREATOR, default OFF),
    # Lead-only. Registered AFTER the role-binding pass so its explicit allowed_for_roles stands.
    from bowhead_core.tool_creator_trigger import register_tool_creator_trigger

    register_tool_creator_trigger(tool_registry)

    # The subagent_creator trigger is Lead-only.
    from bowhead_core.subagent_creator_trigger import register_subagent_creator_trigger

    register_subagent_creator_trigger(tool_registry)
    from bowhead_core.agent_coach import register_agent_coach_tools

    register_agent_coach_tools(tool_registry)

    # workflow-solidify S2: the workflow_creator TRIGGER — flag-gated (BOWHEAD_CCA_WORKFLOW_CREATOR,
    # the SAME single switch that mounts the distill route), Lead-only. Registered after the
    # role-binding pass so its explicit allowed_for_roles stands.
    from bowhead_core.workflow_creator_trigger import register_workflow_creator_trigger

    register_workflow_creator_trigger(tool_registry)

    # Config copilot batch 0.5 (tech-design-batch05-chat.md §3.3): config_read / config_propose, Lead-only,
    # always registered (the FEATURE_CONFIG_COPILOT gate was removed on 2026-08-18). Like the three cca tools,
    # registration follows the role-binding pass so inline allowed_for_roles takes effect.
    from bowhead_core.config_copilot_chat_tools import register_config_copilot_chat_tools

    # Batch 1-a: narrow persistence-interface provider for proposals (resolved at invocation; store initializes later in lifespan,
    # lazy closure decouples ordering; absent store makes handler return propose_failed without emitting).
    from bowhead_gateway.configuration_agent_access import configuration_access_resolver

    register_config_copilot_chat_tools(
        tool_registry,
        proposal_writer_provider=lambda: getattr(app.state, "config_draft_store", None),
        configuration_access_resolver=configuration_access_resolver(app),
    )

    # agent-feedback-loop batch 3c: Lead feedback_escalate tool (chat-named targets enter inbox). Registration-time reads;
    # target names resolve server-side (model supplies only user-recognizable names).
    from bowhead_core.feedback_escalate_tool import register_feedback_escalate_tool

    from bowhead_gateway.feedback_escalate_resolver import resolve_feedback_target

    async def _resolve_feedback_target(team_id: str, kind: str, name: str) -> str | None:
        store = getattr(app.state, "user_store", None)  # Look up at invocation: user_store is built later (UnboundLocalError note below).
        return await resolve_feedback_target(store, team_id, kind, name) if store is not None else None

    register_feedback_escalate_tool(
        tool_registry,
        store_provider=lambda: getattr(app.state, "feedback_candidate_store", None),
        resolver_provider=lambda: _resolve_feedback_target,
    )

    from bowhead_core.current_user_tool import register_current_user_tool
    from bowhead_core.send_email_tool import register_send_email_tool

    register_current_user_tool(tool_registry, store_provider=lambda: getattr(app.state, "user_store", None))

    register_send_email_tool(
        tool_registry,
        store_provider=lambda: getattr(app.state, "user_store", None),
    )

    from bowhead_core.google_delivery_tool import register_google_delivery_tools

    # Google Workspace delivery (Lead-only, same shape as send_email): always registered, unadvertised without enabled team connection/present user.
    register_google_delivery_tools(tool_registry, store_provider=lambda: getattr(app.state, "user_store", None))

    # G5 invariant (docs/skills-tools-ssot.md §5-G5): tool names ∩ skill names = ∅.
    # One root cause of the ω incident: python_sandbox's dual identity induced direct same-name LLM tool calls.
    # Builtin catalog collision = repository error → fail-fast boot failure;
    # machine-level catalogs (~/.bowhead/skills legacy data) log errors without breaking prod;
    # new additions are blocked both ways by reserved names (skill_upload) + this guard.
    _tool_names_now = set(tool_registry.list_names())
    try:
        from bowhead_skill import builtin_skill_names

        _builtin_names = set(builtin_skill_names())  # review-F8: cached, single owner
    except Exception:
        _builtin_names = set()
    _builtin_collisions = _builtin_names & _tool_names_now
    if _builtin_collisions:
        raise RuntimeError(f"tool/skill namespace collision (G5 invariant, builtin catalog): {sorted(_builtin_collisions)}")
    _box_collisions = (set(skill_catalog.list_names()) - _builtin_names) & _tool_names_now
    if _box_collisions:
        log.error("skills.namespace_collision_box_level names=%s (G5 — 请改名该目录 skill)", sorted(_box_collisions))

    # i18n v1: load product-terms catalog once at startup; prompt builders inject subsets.
    # Failures do not block startup: omit glossary segments (directive body still applies).
    product_terms_catalog: Any | None = None
    try:
        from bowhead_core.product_terms import default_catalog_path, load_catalog

        catalog_path = default_catalog_path()
        if catalog_path.exists():
            product_terms_catalog = load_catalog(catalog_path)
            log.info("product_terms_catalog.loaded path=%s terms=%d", catalog_path, len(product_terms_catalog.terms))
        else:
            log.warning("product_terms_catalog.missing path=%s", catalog_path)
    except Exception as _e:
        log.warning("product_terms_catalog.load_failed err=%s", _e)

    # i18n v1: load error catalog (HTTP exception handler renders system errors in user ui_language).
    error_catalog: Any | None = None
    try:
        from bowhead_core.error_catalog import (
            default_error_catalog_dir,
            load_error_catalog,
        )

        ec_dir = default_error_catalog_dir()
        if ec_dir.exists():
            error_catalog = load_error_catalog(ec_dir)
            log.info("error_catalog.loaded dir=%s langs=%d", ec_dir, len(error_catalog.by_lang))
        else:
            log.warning("error_catalog.missing dir=%s", ec_dir)
    except Exception as _e:
        log.warning("error_catalog.load_failed err=%s", _e)
    app.state.error_catalog = error_catalog

    # ─── Orchestrator ─────────────────────────────────────────
    orchestrator = Orchestrator(
        router=router,
        datarc=None,
        tool_registry=tool_registry,
        event_registry=get_event_registry(),
        config=OrchestratorConfig(),
        # Explicitly pin embedding collection tenant (empty = infer from connection). Previously absent: settings had the field
        # and comments claimed Gateway lifespan passed it explicitly, but it was never wired,
        # so every deployment constructed collection names using Orchestrator's default.
        datarc_tenant=settings.vector_db_tenant,
        user_memory_store=memory_store,
        session_store=session_store,
        canvas_store=canvas_store,
        approval_store=approval_store,
        dreaming_light=(dreaming_light if dreaming_enabled else None),
        # V3.5 PRD-C wiring
        nl_memory_intent_classifier=(
            __import__(
                "bowhead_core.nl_memory_intent",
                fromlist=["NLMemoryIntentClassifier"],
            ).NLMemoryIntentClassifier(router=router)
            if nl_intent_enabled
            else None
        ),
        memory_candidate_store=memory_candidate_store,
        memory_notes_store=memory_notes_store,
        nl_intent_enabled=nl_intent_enabled,
        # V4 PRD-D-4-4
        memory_pack_snapshots_store=memory_pack_snapshots_store,
        memory_pack_snapshots_enabled=memory_pack_snapshots_enabled,
        space_instructions_store=space_instructions_store,
        # V5 PRD-E2
        inline_preview_enabled=inline_preview_enabled,
        metrics_log=metrics_log,
        vector_client=vector_client,
        artifact_store=artifact_store,
        skill_instance_store=skill_instance_store,
        skill_catalog=skill_catalog,
        custom_subagent_store=custom_subagent_store,
        # M156
        schema_cache=schema_cache,
        clarification_store=clarification_store,
    )
    orchestrator.dispatch_owner_id = _chat_dispatch_owner_id
    # i18n v1: attach catalog to orch for turn_runner to forward to PromptBuilder.
    orchestrator.product_terms_catalog = product_terms_catalog  # type: ignore[attr-defined]

    # ─── Multi-agent v1.5 wire-up (sprint multi-agent-v1.5 PR-#19) ────────
    # Feature flag defaults OFF → maybe_run_via_v2 returns None → legacy path.
    # Bootstrap is idempotent + lightweight; always run at startup, ready for flag flips.
    try:
        from bowhead_core.agents.turn_runner_v2_entry import bootstrap_multi_agent

        multi_agent_registry = bootstrap_multi_agent(tool_registry)
        orchestrator.multi_agent_registry = multi_agent_registry  # type: ignore[attr-defined]

        # tool-domain (D1): seed source/annotations on every built-in now that ALL
        # register_* have run (incl. creator-triggers above + bootstrap_multi_agent).
        # Per-turn views clone the base, inheriting these. team/MCP tools self-classify via
        # the tool_category.lookup_domain floor; annotation is a fast-path cache.
        from bowhead_core.tool_registry import apply_tool_annotations

        apply_tool_annotations(tool_registry)

        # WORK manifest router LLM — wrap router.call_llm
        async def _multi_agent_router_llm_fn(prompt: str) -> dict:
            """Wrap router.call_llm for orchestrate_v2 manifest-driven WORK router."""
            from bowhead_router.types import LLMRequest, Message

            req = LLMRequest(
                model="skill",  # SKILL capability (Haiku/Qwen-flash, ~200ms)
                messages=[Message(role="user", content=prompt)],
                max_output_tokens=1500,
                response_format={"type": "json_object"},
            )
            resp = await router.call_llm(req, agent_type="skill")
            import json

            try:
                return json.loads(resp.content or "{}")
            except json.JSONDecodeError:
                return {}

        orchestrator.multi_agent_router_llm_fn = _multi_agent_router_llm_fn  # type: ignore[attr-defined]

        # Sprint θ-Phase-0c (2026-05-08): missing wiring; v2 hook checks
        # orch.subagent_runner, but v1.5 PR-#19 omitted assignment, causing 100% of v2 calls since shipping to return
        # outcome=subagent_missing, silently falling back to legacy. This one-line wire
        # actually enables v2. SubagentRunner is a reusable singleton (per
        # turn_runner._make_subagent_runner_with_catalog: holds only router +
        # tool_registry dependencies, no per-turn state).
        from bowhead_core.turn_runner import _make_subagent_runner_with_catalog

        orchestrator.subagent_runner = _make_subagent_runner_with_catalog(  # type: ignore[attr-defined]
            router=router,
            tool_registry=tool_registry,
            custom_store=getattr(orchestrator, "custom_subagent_store", None),
            product_terms_catalog=getattr(orchestrator, "product_terms_catalog", None),
        )

        # H2 approval service: wire emit/wait_decision to WebSocket bus (PR-#20).
        # Default no service → HIGH complexity dispatches directly without H2 approval;
        # enable actual H2 approval after PR-#20 wires WebSocket.
        orchestrator.task_plan_service = None  # type: ignore[attr-defined]

        # _build_tool_exec_ctx hook delegates to turn_runner's internal context construction.
        # Current stub returns minimal ToolExecutionContext (turn_runner's internal factory is
        # more complex, to be exposed in later PRs).
        log.info("multi_agent.bootstrapped registry_count=%d", len(multi_agent_registry.list_all()))
    except Exception as _e:
        log.warning("multi_agent.bootstrap_failed err=%s", _e)
        orchestrator.multi_agent_registry = None  # type: ignore[attr-defined]
        orchestrator.multi_agent_router_llm_fn = None  # type: ignore[attr-defined]
    # ────────────────────────────────────────────────────────────────────

    # cca P3 F-B: the artifact-fetch signing secret reachable from turn_runner (via orch)
    # to build the per-turn ctx-minter closure, and from the download route (via app.state)
    # to verify the runner's Bearer. Same value both sides.
    orchestrator.artifact_fetch_secret = _artifact_fetch_secret  # type: ignore[attr-defined]

    # credential-readiness R6: attach the same per-team search/media resolver closures to orch;
    # turn_runner uses them when creating context to calculate unconfigured_capability_tools (hide unconfigured optional
    # capabilities from LLM advertisements, same source as handlers' invocation-time resolution). Attach unconditionally.
    orchestrator.team_search_resolver = _team_search_resolver  # type: ignore[attr-defined]
    orchestrator.team_media_resolver = _team_media_resolver  # type: ignore[attr-defined]
    # Attach only with Tavily web tools enabled: per-turn visibility checks query/decrypt; OFF has no registered tools, so avoid that cost.
    from bowhead_core.tavily_tools_flags import tavily_tools_enabled

    if tavily_tools_enabled():
        orchestrator.team_tavily_resolver = _team_tavily_credential  # type: ignore[attr-defined]

    app.state.settings = settings
    app.state.provider_registry = provider_registry
    # Batch 2-a C7b: app.state.router removed. No production readers remain; all 8 sites use
    # resolve_team_router/team_router_or_409. After removal, getattr(app.state,
    # "router", None) always yields None (correct skip-LLM degradation); direct reads raise AttributeError
    # loudly. Assembly routers' declared identity (§3.2) actually blocks new bypasses; field removal supports this structurally.
    # Tests may still setattr independently (as poison doubles do).
    app.state.orchestrator = orchestrator
    orchestrator.plan_store = plan_store  # type: ignore[attr-defined]  # planning-mode M1 (block 4)
    from bowhead_gateway.feishu.bridge import FeishuTurnBridge
    from bowhead_gateway.feishu.client import FeishuClient
    from bowhead_gateway.feishu.config import (
        DbBackedFeishuAppConfigResolver,
        long_connection_restart_signature,
    )
    from bowhead_gateway.feishu.long_connection import FeishuLongConnectionWorker
    from bowhead_gateway.feishu.security import FeishuSecurityConfig
    from bowhead_gateway.feishu.store import FeishuEventStore

    feishu_event_store = FeishuEventStore(db_path)
    await feishu_event_store.initialize()
    feishu_app_config_resolver = DbBackedFeishuAppConfigResolver(settings, str(db_path))
    _feishu_hygiene_task: asyncio.Task[None] | None = None
    feishu_hygiene_interval_sec = max(300, settings.feishu_sweep_interval_sec)

    async def _feishu_hygiene_loop() -> None:
        await asyncio.sleep(60)
        while True:
            try:
                if not feishu_app_config_resolver.resolve("").enabled:
                    await asyncio.sleep(feishu_hygiene_interval_sec)
                    continue
                stats = await feishu_event_store.sweep_hygiene(
                    dedup_retention_days=settings.feishu_dedup_retention_days,
                    turn_link_retention_days=settings.feishu_turn_link_retention_days,
                    session_retention_days=settings.feishu_session_retention_days,
                )
                if stats.total_mutations:
                    log.info("feishu.hygiene_sweep", **stats.to_log_fields())
                await asyncio.sleep(feishu_hygiene_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("feishu.hygiene_sweep_failed", error=str(e))
                await asyncio.sleep(feishu_hygiene_interval_sec)

    _feishu_hygiene_task = asyncio.create_task(_feishu_hygiene_loop())
    feishu_app_config = feishu_app_config_resolver.resolve("")
    feishu_config = FeishuSecurityConfig(
        verification_token=feishu_app_config.verification_token,
        encrypt_key=feishu_app_config.encrypt_key,
        bot_open_id=feishu_app_config.bot_open_id,
        group_public_answers_enabled=feishu_app_config.group_public_answers_enabled,
    )
    feishu_client = FeishuClient(
        feishu_app_config_resolver,
        base_url=settings.feishu_openapi_base_url,
    )
    app.state.feishu_app_config_resolver = feishu_app_config_resolver
    app.state.feishu_security_config = feishu_config
    app.state.feishu_config = feishu_config
    app.state.feishu_event_store = feishu_event_store
    app.state.feishu_client = feishu_client
    app.state.feishu_accepting_dispatch = True
    app.state.feishu_long_connection_runtime = {
        "worker_signature": None,
        "restart_required": False,
        "message": None,
        "changed_fields": [],
    }
    feishu_bridge = FeishuTurnBridge(
        app=app,
        client=feishu_client,
        store=feishu_event_store,
        config=feishu_config,
        app_config_resolver=feishu_app_config_resolver,
    )
    app.state.feishu_bridge = feishu_bridge
    app.state.feishu_long_connection_worker = None
    if feishu_app_config.enabled and feishu_app_config.event_ingress == "long_connection":
        feishu_long_connection_worker = FeishuLongConnectionWorker(
            bridge=feishu_bridge,
            store=feishu_event_store,
            app_config_resolver=feishu_app_config_resolver,
            base_url=settings.feishu_openapi_base_url,
        )
        app.state.feishu_long_connection_worker = feishu_long_connection_worker
        app.state.feishu_long_connection_runtime["worker_signature"] = long_connection_restart_signature(feishu_app_config)

    # Teams P0 configuration is team-scoped and persisted by Alembic.  Unlike
    # Feishu's historical adapter it intentionally has no process-env fallback.
    from bowhead_gateway.teams.store import TeamsStore

    app.state.teams_store = TeamsStore(db_path)
    # Slack uses the same team-scoped, Alembic-managed credential custody as
    # Teams.  Its public callbacks are still enabled only in local deployments
    # by the route-level deployment gate.
    from bowhead_gateway.slack.store import SlackStore

    app.state.slack_store = SlackStore(db_path)

    app.state.user_memory_store = memory_store
    # M158-10 paradigm v2: project_memory_store aggregates across users (shared user_memory SQLite + schema).
    from bowhead_core.user_memory import ProjectMemoryStore

    project_memory_store = ProjectMemoryStore(db_path)
    await project_memory_store.initialize()
    app.state.project_memory_store = project_memory_store
    # ws-datarc-decouple P3 (R8): expose on the orchestrator so turn_runner can thread it onto
    # ToolExecutionContext — the bi tools write the per-turn datarc project as "last used".
    orchestrator.project_memory_store = project_memory_store  # type: ignore[attr-defined]
    # ── workspace-datarc-decouple P2a (③): team datarc connection registry + per-conn pool ──
    # The gateway resolves the CALLER team's connection at turn-start (chat.py) and threads the
    # resulting client onto the TurnRequest. A team without a saved connection has no Datarc
    # capability; the runner never falls back to a deployment-level connection.
    datarc_connection_store = DatarcConnectionStore(db_path)
    await datarc_connection_store.initialize()
    app.state.datarc_connection_store = datarc_connection_store
    # Power BI Service Principal configuration is team-scoped. Schema creation is handled by
    # Alembic; query-time capability is available only to teams with an active connection.
    app.state.datarc_client_factory = DatarcClientFactory()
    # embed-via-openapi: per-connection datarc-proxy VectorClient pool (BI semantic dictionary
    # via the team's connection key, `/openapi/v1/embed/`). Resolution in chat.py mirrors the
    # datarc client above; teams without a connection keep the global direct embed client.
    embed_client_factory = EmbedProxyClientFactory(
        timeout_sec=settings.vector_db_timeout_sec,
    )
    app.state.embed_client_factory = embed_client_factory
    # M158-18 phase 4: periodic project_memory cron aggregation task, fire-and-forget,
    # canceled on main-process exit. Frequency: BOWHEAD_PROJECT_MEMORY_CRON_HOURS (default 168h).
    from bowhead_core.project_memory_aggregator import start_project_memory_cron

    app.state._project_memory_cron_task = start_project_memory_cron(
        memory_store,
        project_memory_store,
    )
    app.state.session_store = session_store
    app.state.canvas_store = canvas_store
    app.state.export_snapshot_store = export_snapshot_store
    app.state.export_store = export_store
    app.state.export_processor_registry = export_processor_registry
    app.state.export_runner = export_runner
    app.state.plan_store = plan_store  # planning-mode M1 (block 4)
    app.state.approval_store = approval_store
    app.state.artifact_store = artifact_store
    # Sprint file-upload-presign Phase 1.2: receiver route looks these up.
    # Secret reuses the value computed above (review P0-S-1) so signed JWTs
    # verify against the same key.
    app.state.artifact_backend = artifact_backend
    app.state.local_presign_secret = _local_presign_secret
    app.state.artifact_fetch_secret = _artifact_fetch_secret  # cca P3 F-B (download verify)
    app.state.turn_log_store = turn_log_store
    app.state.scheduled_store = scheduled_store
    app.state.custom_subagent_store = custom_subagent_store
    app.state.builtin_skill_reference_metadata = builtin_skill_reference_metadata
    from bowhead_core.skill_read_resolution import build_reference_skill_sources

    orchestrator.reference_skill_sources_factory = lambda scope: build_reference_skill_sources(
        db_path, scope=scope, builtin_index=lambda: builtin_skill_reference_metadata
    )
    app.state.uploaded_skill_reference_metadata = uploaded_skill_reference_metadata
    app.state.skill_catalog = skill_catalog
    app.state.skill_runtime = skill_runtime

    # V2.1.5: user_settings_store + V3 PRD-B: metrics_log + pause_state_cache
    # (instances built early in this lifespan so DreamingLight can take pause_cache)
    app.state.user_settings_store = user_settings_store
    app.state.personal_appearance_store = personal_appearance_store
    app.state.personal_appearance_service = personal_appearance_service
    app.state.home_proposal_cache_store = home_proposal_cache_store
    app.state.team_settings_store = team_settings_store
    app.state.chart_style_store = chart_style_store
    app.state.chart_style_read_service = chart_style_read_service
    app.state.chart_style_uow = chart_style_uow
    app.state.metrics_log = metrics_log
    app.state.pause_state_cache = pause_state_cache
    # V3.5 PRD-C
    app.state.memory_notes_store = memory_notes_store
    app.state.nl_intent_enabled = nl_intent_enabled
    app.state.dreaming_enabled = dreaming_enabled
    # V4 PRD-D-4-4
    app.state.memory_pack_snapshots_store = memory_pack_snapshots_store
    app.state.space_instructions_store = space_instructions_store
    app.state.memory_pack_snapshots_enabled = memory_pack_snapshots_enabled
    # V4 PRD-D-5
    app.state.memory_from_canvas_enabled = memory_from_canvas_enabled
    # V4 Sprint 2 PRD-D-4-1
    app.state.memory_note_anchors_store = memory_note_anchors_store
    app.state.anchor_invalidation_enabled = anchor_invalidation_enabled
    # V5 PRD-E2
    app.state.inline_preview_enabled = inline_preview_enabled

    # KB model runtime derives only from team settings; initialize shared file/index storage here.
    _gw_logger = logging.getLogger("bowhead_gateway.main")
    from bowhead_core.kb.files_store import KbFilesStore as _KbFilesStore
    from bowhead_core.kb.store import KbStore as _KbStore

    _kb_chromadb_dir = os.environ.get(
        "BOWHEAD_KB_CHROMADB_DIR",
        str(Path(db_path).parent / "kb_chromadb"),
    )
    _kb_files_store = _KbFilesStore(db_path=Path(db_path).parent / "kb_files.db")
    await _kb_files_store.initialize()
    # KB UX remediation FR1: process startup timestamp. Startup recovery touches only rows with older heartbeats
    # (previous-process residue); rows written by this process have newer heartbeats, avoiding false orphan classification.
    from bowhead_core.kb.parse_recovery import heartbeat_iso as _kb_heartbeat_iso

    _kb_parse_boot_at = _kb_heartbeat_iso()
    # kb-structured-tables Sprint 2 (Phase A): ALTER ADD COLUMN
    # auto_classified_doc_type_id (idempotent, soft-fail).
    from bowhead_core.kb.doc_type_migration import (
        migrate_kb_doc_types_add_columns as _migrate_kb_doc_types_add_columns,
    )
    from bowhead_core.kb.doc_type_migration import (
        migrate_kb_files_add_auto_classified as _migrate_kb_files_add_auto_classified,
    )

    await _migrate_kb_files_add_auto_classified(
        Path(db_path).parent / "kb_files.db",
    )
    # kb-knowledge-tables sprint v1.0: doc_type (extract_schema) store + extracted_record store
    from bowhead_core.kb.doc_type_store import KbDocTypeStore as _KbDocTypeStore
    from bowhead_core.kb.extracted_record_store import (
        KbExtractedRecordStore as _KbExtractedRecordStore,
    )
    from bowhead_core.kb.extracted_record_store import (
        migrate_v1_to_v2 as _migrate_kb_records_v1_to_v2,
    )

    _kb_records_db_path = Path(db_path).parent / "kb_extracted_records.db"
    _kb_doc_types_db_path = Path(db_path).parent / "kb_doc_types.db"
    # kb-v2 §A.6: doc_type_store hooks _rebuild_field_indexes on records DB
    # after CRUD. Records DB is a sibling SQLite file.
    _kb_extracted_rows_db_path_for_dts = Path(db_path).parent / "kb_extracted_rows.db"
    _kb_doc_type_store = _KbDocTypeStore(
        db_path=_kb_doc_types_db_path,
        records_db_path=_kb_records_db_path,
        rows_db_path=_kb_extracted_rows_db_path_for_dts,  # Sprint 25 P0-PERF-1
    )
    await _kb_doc_type_store.initialize()
    # kb-structured-tables Sprint 2 (Phase A): ALTER ADD COLUMN x3 to kb_doc_types
    # (cardinality / auto_extract_enabled / sample_keywords_json). Idempotent.
    await _migrate_kb_doc_types_add_columns(_kb_doc_types_db_path)
    # kb-per-row-extract Sprint 25 (Phase C): ALTER ADD COLUMN x2 to kb_doc_types
    # (row_count / dedup_key_fields_json). Idempotent.
    from bowhead_core.kb.doc_type_migration import (
        migrate_kb_doc_types_add_sprint25_columns as _migrate_kb_doc_types_sprint25,
    )

    await _migrate_kb_doc_types_sprint25(_kb_doc_types_db_path)
    # kb-matrix M4 (FR-40): ALTER ADD COLUMN x3 to kb_doc_types (graduation lineage:
    # graduated_from_matrix_id / graduated_from_seq / graduated_mode）。Idempotent。
    from bowhead_core.kb.doc_type_migration import (
        migrate_kb_doc_types_add_m4_columns as _migrate_kb_doc_types_m4,
    )

    await _migrate_kb_doc_types_m4(_kb_doc_types_db_path)
    # kb-matrix-sharing: ALTER ADD COLUMN copied_from_team_template_id (team-template copy
    # lineage). **Always run**: create()'s shared INSERT needs this column on every deployment (fresh databases get it
    # from _SCHEMA; this migration covers legacy databases). Idempotent.
    from bowhead_core.kb.doc_type_migration import (
        migrate_kb_doc_types_add_sharing_columns as _migrate_kb_doc_types_sharing,
    )

    await _migrate_kb_doc_types_sharing(_kb_doc_types_db_path)
    # kb-matrix-sharing: team-template registry store; source deletion cascades via delete_doc_type hooks
    # to remove registry entries synchronously, preventing dangling live entries.
    from bowhead_core.kb.team_template_store import KbTeamTemplateStore as _KbTeamTemplateStore

    _kb_team_templates_db_path = Path(db_path).parent / "kb_team_templates.db"
    _kb_team_template_store = _KbTeamTemplateStore(db_path=_kb_team_templates_db_path)
    await _kb_team_template_store.initialize()
    app.state.kb_team_template_store = _kb_team_template_store
    _kb_extracted_record_store = _KbExtractedRecordStore(db_path=_kb_records_db_path)
    await _kb_extracted_record_store.initialize()
    # kb-knowledge-tables v2 sprint: schema v1→v2 idempotent migration.
    await _migrate_kb_records_v1_to_v2(_kb_records_db_path)

    # ─── kb-per-row-extract Sprint 25 (Phase C): row store + jobs store + worker DI ──
    # Independent SQLite db files (rollback-safe — 0.43.0 image doesn't open them).
    from bowhead_core.kb.extracted_rows_store import (
        KbExtractedRowsStore as _KbExtractedRowsStore,
    )
    from bowhead_core.kb.per_row_jobs_store import (
        KbPerRowJobsStore as _KbPerRowJobsStore,
    )
    from bowhead_core.kb.row_extractor import (
        RowExtractor as _RowExtractor,
    )
    from bowhead_core.kb.table_parser import (
        TableParserDispatcher as _TableParserDispatcher,
    )

    _kb_extracted_rows_db_path = Path(db_path).parent / "kb_extracted_rows.db"
    _kb_per_row_jobs_db_path = Path(db_path).parent / "kb_per_row_jobs.db"

    _kb_extracted_rows_store = _KbExtractedRowsStore(db_path=_kb_extracted_rows_db_path)
    await _kb_extracted_rows_store.initialize()

    _kb_per_row_jobs_store = _KbPerRowJobsStore(db_path=_kb_per_row_jobs_db_path)
    await _kb_per_row_jobs_store.initialize()

    # Sprint 26: per-ws monthly LLM token quota store (co-located w/ jobs db)
    from bowhead_core.kb.workspace_quotas_store import (
        KbWorkspaceQuotasStore as _KbWorkspaceQuotasStore,
    )

    _kb_workspace_quotas_store = _KbWorkspaceQuotasStore(db_path=_kb_per_row_jobs_db_path)
    await _kb_workspace_quotas_store.initialize()
    app.state.kb_workspace_quotas_store = _kb_workspace_quotas_store

    # RowExtractor DI: table_parser (pdfplumber + L2 vision dispatcher)
    _kb_row_extractor = _RowExtractor(table_parser=_TableParserDispatcher())

    # app.state strong-ref dict for active per-row tasks (Sprint 2 backfill pattern)
    app.state.kb_per_row_tasks_by_file_id = {}
    app.state.team_kb_reindex_tasks = {}
    app.state.kb_extracted_rows_store = _kb_extracted_rows_store
    app.state.kb_per_row_jobs_store = _kb_per_row_jobs_store
    app.state.kb_row_extractor = _kb_row_extractor

    # Sprint 25: ALTER kb_files ADD COLUMN extracted_rows_status (idempotent)
    # is integrated into KbFilesStore.initialize() via _migrate_extracted_rows_status

    # Sprint 25: heartbeat scanner — every 10 min, mark jobs without heartbeat
    # >15min as failed_retry (so scanner can resume later). Per tech-design §C8.
    async def _kb_per_row_heartbeat_scanner() -> None:
        gw_logger = logging.getLogger("bowhead_gateway.main")
        while True:
            try:
                await asyncio.sleep(600)  # 10 min
                count = await _kb_per_row_jobs_store.mark_zombies(threshold_minutes=15)
                if count > 0:
                    gw_logger.info("kb_per_row heartbeat scanner marked %d zombies", count)
            except asyncio.CancelledError:
                break
            except Exception:
                gw_logger.exception("kb_per_row heartbeat scanner error (loop continues)")
                # Continue loop on error — don't let scanner die
                await asyncio.sleep(60)

    app.state._kb_per_row_heartbeat_task = asyncio.create_task(_kb_per_row_heartbeat_scanner())
    # kb-structured-tables Sprint 2 (Phase A): lifespan marker store + catalog seed
    # fire-and-forget background task. Marker store DB is co-located with
    # kb_doc_types.db per tech-design Q-T7.
    from bowhead_core.kb.catalog_seed import seed_catalog_for_ws as _seed_catalog_for_ws
    from bowhead_core.kb.lifespan_marker_store import KbLifespanMarkerStore as _KbLifespanMarkerStore

    _kb_marker_store = _KbLifespanMarkerStore(db_path=_kb_doc_types_db_path)
    await _kb_marker_store.initialize()
    app.state.kb_marker_store = _kb_marker_store

    async def _backfill_all_ws_catalog() -> None:
        """Background task: seed catalog for every distinct workspace.

        Paced 100ms between ws to avoid SQLite lock starvation. Per-ws
        idempotent via marker table; safe to run on every cold boot.
        """
        gw_logger = logging.getLogger("bowhead_gateway.main")
        try:
            ws_ids = await _kb_files_store.list_distinct_workspaces()
            gw_logger.info("catalog seed background: %d distinct workspaces", len(ws_ids))
            for ws_id in ws_ids:
                try:
                    await _seed_catalog_for_ws(
                        ws_id,
                        _kb_doc_type_store,
                        _kb_marker_store,
                    )
                except Exception:
                    gw_logger.exception("catalog seed failed ws=%r (continuing)", ws_id)
                await asyncio.sleep(0.1)
        except Exception:
            gw_logger.exception("catalog seed backfill loop crashed")

    app.state._kb_catalog_seed_task = asyncio.create_task(_backfill_all_ws_catalog())
    app.state.kb_doc_type_store = _kb_doc_type_store
    app.state.kb_extracted_record_store = _kb_extracted_record_store
    _kb_store = _KbStore(persist_dir=_kb_chromadb_dir)
    # Without team KB configuration, models remain unavailable; never fall back to deployment-level keys.
    _kb_retriever = None
    _kb_worker = None
    app.state.kb_files_store = _kb_files_store
    app.state.kb_store = _kb_store
    app.state.kb_retriever = _kb_retriever
    app.state.kb_worker = _kb_worker
    # P4.5.2 [0.51.0 merge-fix]: build the per-team Router cache BEFORE TeamKbRuntimeRegistry
    # consumes app.state.team_router_registry just below. The 0.50.0→decouple merge ordered the
    # consumer (TeamKbRuntimeRegistry, reads team_router_registry) ~140 lines ahead of the producer
    # → AttributeError: 'State' object has no attribute 'team_router_registry' → startup crash-loop.
    from bowhead_core.team_router_registry import TeamRouterRegistry as _TeamRouterRegistry

    _team_router_registry = _TeamRouterRegistry()
    await _team_router_registry.start_background()
    app.state.team_router_registry = _team_router_registry
    from bowhead_core.kb.ingestion_gate import TeamKbIngestionGate as _TeamKbIngestionGate
    from bowhead_core.team_kb_runtime import TeamKbRuntimeRegistry as _TeamKbRuntimeRegistry

    from bowhead_gateway.knowledge_management import ingestion_authorizer

    _team_kb_ingestion_gate = _TeamKbIngestionGate()
    app.state.team_kb_ingestion_gate = _team_kb_ingestion_gate
    _team_kb_runtime_registry = _TeamKbRuntimeRegistry(
        persist_dir=_kb_chromadb_dir,
        files_store=_kb_files_store,
        artifact_store=artifact_store,
        router=orchestrator.router,
        team_router_registry=app.state.team_router_registry,
        doc_type_store=_kb_doc_type_store,
        extracted_record_store=_kb_extracted_record_store,
        extracted_rows_store=_kb_extracted_rows_store,
        per_row_jobs_store=_kb_per_row_jobs_store,
        row_extractor=_kb_row_extractor,
        active_per_row_tasks_ref=app.state.kb_per_row_tasks_by_file_id,
        ingestion_gate=_team_kb_ingestion_gate,
        ingestion_authorizer=ingestion_authorizer(app),
    )
    await _team_kb_runtime_registry.start_background()
    app.state.team_kb_runtime_registry = _team_kb_runtime_registry
    # P4.5.1 [0.51.0 merge-fix]: User/Team/credit/audit store — created HERE (before its first
    # straight-line use just below in _resume_pending_embed_reindexes). The 0.50.0→decouple merge
    # left the `user_store = UserStore(...)` assignment ~80 lines later (~L1281), so Python treated
    # `user_store` as a local referenced-before-assignment → UnboundLocalError at startup. Moved up.
    from bowhead_core.user_store import UserStore

    user_store = UserStore(
        db_path,
        terminal_auth_fence_enabled=(getattr(app.state, "terminal_protocol_store", None) is not None),
    )
    await user_store.initialize()
    from bowhead_core.account_deletion import adopt_deleted_accounts
    from bowhead_core.account_deletion_ledger import AccountDeletionLedger

    account_deletion_ledger = None
    if _privacy_ledger_path and _privacy_ledger_key:
        account_deletion_ledger = AccountDeletionLedger(
            Path(_privacy_ledger_path).with_name("account-deletions.db"),
            secret=_privacy_ledger_key,
        )
        await account_deletion_ledger.initialize()
        user_store.account_deletion_recorder = account_deletion_ledger.record
        # Apply retirement intents before starting authenticated services or
        # scheduling work against a restored business database.
        await account_deletion_ledger.replay(user_store)
        while await adopt_deleted_accounts(user_store):
            pass
    else:
        async with aiosqlite.connect(db_path) as account_db:
            async with account_db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='account_deletions'") as cursor:
                has_account_deletions = await cursor.fetchone() is not None
            if has_account_deletions:
                async with account_db.execute("SELECT 1 FROM account_deletions LIMIT 1") as cursor:
                    if await cursor.fetchone() is not None:
                        raise RuntimeError("Enrolled account deletions require the latest external privacy ledger and BOWHEAD_FERNET_KEY")
    app.state.user_store = user_store
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_automation_context import require_protocol_schema_conn

    async with open_shared_ctx(db_path) as protocol_db:
        await require_protocol_schema_conn(protocol_db)

    # agent-feedback-loop batch 2b-i: candidate-pool store (migration 0059 creates table; dark);
    # adding keys later changes signing semantics, invalidating existing cooldowns/unique keys (store __post_init__ documents this).
    from bowhead_core.feedback_candidate_store import FeedbackCandidateStore

    app.state.feedback_candidate_store = FeedbackCandidateStore(
        db_path,
        fingerprint_key=os.environ.get("BOWHEAD_FERNET_KEY") or None,
    )
    from bowhead_core.feedback_casework_store import FeedbackCaseworkStore

    app.state.feedback_casework_store = FeedbackCaseworkStore(db_path)  # Table created by migration 0062.
    # Local-only Team OpenAPI credentials share the primary SQLite database.
    # Tables are Alembic-owned; this object contains no startup DDL.
    from bowhead_core.openapi_store import OpenApiStore

    app.state.openapi_store = OpenApiStore(db_path)
    openapi_log_prune_task: asyncio.Task[None] | None = None
    # Subagent management compatibility + conversational draft reader/writer
    # admission. New authoring behavior remains behind stable team-cohort
    # flags; schema presence alone never opens the feature.
    from bowhead_core.subagent_compatibility import (
        TeamSubagentCompatibilityStore,
    )
    from bowhead_core.team_subagent_store import (
        AGENT_RELEASE_COMPATIBILITY_VERSION,
        TeamSubagentStore,
    )

    from bowhead_gateway.subagent_compatibility import (
        GatewaySubagentCompatibilityHeartbeat,
        new_gateway_compatibility_instance_id,
    )

    team_subagent_schema_store = TeamSubagentStore(db_path)
    await team_subagent_schema_store.initialize()
    # Keep this distinct from the existing runtime's ``team_subagent_store``
    # field, which still expects legacy UserStore CRUD throughout Release A.
    app.state.team_subagent_schema_store = team_subagent_schema_store
    from bowhead_core.agent_assignment_store import AgentAssignmentStore

    agent_assignment_store = AgentAssignmentStore(db_path)
    app.state.agent_assignment_store = agent_assignment_store
    try:
        await agent_assignment_store.initialize()
    except Exception as exc:
        app.state.agent_marketplace_schema_ready = False
        log.error("agent_marketplace.schema_not_ready", error=str(exc))
    else:
        app.state.agent_marketplace_schema_ready = True
    from bowhead_core.agent_role_preference import AgentRolePreferenceStore

    agent_role_preference_store = AgentRolePreferenceStore(db_path)
    app.state.agent_role_preference_store = agent_role_preference_store
    app.state.agent_role_preference_schema_ready = False
    try:
        await agent_role_preference_store.initialize()
    except Exception as exc:
        log.error("agent_role_preference.schema_not_ready", error=str(exc))
    else:
        app.state.agent_role_preference_schema_ready = True
    team_subagent_compatibility_store = TeamSubagentCompatibilityStore(db_path)
    subagent_compatibility_heartbeat = GatewaySubagentCompatibilityHeartbeat(
        team_subagent_compatibility_store,
        instance_id=new_gateway_compatibility_instance_id(),
        reader_version=AGENT_RELEASE_COMPATIBILITY_VERSION,
        writer_version=AGENT_RELEASE_COMPATIBILITY_VERSION,
    )
    compatibility_decision = await subagent_compatibility_heartbeat.admit()
    app.state.subagent_compatibility_heartbeat = subagent_compatibility_heartbeat
    subagent_compatibility_task = asyncio.create_task(
        subagent_compatibility_heartbeat.run(),
        name="subagent-compatibility-heartbeat",
    )
    app.state.subagent_compatibility_task = subagent_compatibility_task
    log.info(
        "subagent.compatibility_admitted",
        reader_version=compatibility_decision.reader_version,
        writer_version=compatibility_decision.writer_version,
        minimum_reader_version=(compatibility_decision.minimum_reader_version),
        minimum_writer_version=(compatibility_decision.minimum_writer_version),
    )
    # Conversational draft tables are Alembic-owned. Stores and maintenance
    # always start so read/delete/retention keep working. New idempotent
    # generation needs the HMAC key, derived from the JWT keyring, only while
    # the creator kill-switch leaves authoring enabled. Without a JWT keyring
    # the store stays claim-read-only and chat falls back to the manual card.
    from bowhead_core.subagent_authoring_context_store import (
        SubagentAuthoringContextStore,
    )
    from bowhead_core.subagent_creator_trigger import subagent_creator_enabled
    from bowhead_core.subagent_draft_accounting import (
        SubagentDraftCreditAccountant,
    )
    from bowhead_core.subagent_draft_store import (
        SubagentDraftSchemaUnavailableError,
        SubagentDraftStore,
    )

    from bowhead_gateway.subagent_draft_maintenance import (
        run_subagent_draft_maintenance,
    )
    from bowhead_gateway.subagent_draft_runtime import (
        subagent_draft_hmac_key,
    )

    subagent_draft_hmac: bytes | None = None
    if subagent_creator_enabled():
        try:
            subagent_draft_hmac = subagent_draft_hmac_key()
        except RuntimeError:
            log.warning("subagent_draft.hmac_key_unavailable")
    subagent_draft_store = SubagentDraftStore(db_path, hmac_key=subagent_draft_hmac)
    subagent_draft_maintenance_task = None
    try:
        await subagent_draft_store.initialize()
    except SubagentDraftSchemaUnavailableError:
        # A 1.0.0 database may start before the external migration command
        # upgrades it. There can be no existing drafts to retain before this
        # schema exists; chat falls back to the manual card meanwhile.
        log.warning("subagent_draft.runtime_skipped_schema_absent")
    else:
        subagent_authoring_context_store = SubagentAuthoringContextStore(db_path)
        subagent_draft_credit_accountant = SubagentDraftCreditAccountant(db_path)
        await subagent_authoring_context_store.initialize()
        await subagent_draft_credit_accountant.initialize()
        app.state.subagent_draft_store = subagent_draft_store
        app.state.subagent_authoring_context_store = subagent_authoring_context_store
        orchestrator.subagent_authoring_context_store = (  # type: ignore[attr-defined]
            subagent_authoring_context_store
        )
        app.state.subagent_draft_credit_accountant = subagent_draft_credit_accountant
        subagent_draft_maintenance_task = asyncio.create_task(
            run_subagent_draft_maintenance(
                draft_store=subagent_draft_store,
                context_store=subagent_authoring_context_store,
                accountant=subagent_draft_credit_accountant,
            ),
            name="subagent-draft-maintenance",
        )
        app.state.subagent_draft_maintenance_task = subagent_draft_maintenance_task

    # Configuration copilot batch 1-a (tech-design-batch1-draft-store.md §2/§10-3): lite proposal storage.
    # Initialize only with flag ON; missing schema (unmigrated test/dev DB) tolerantly skips without breaking startup. Production images
    # require bowhead migrate upgrade head before startup. Missing store → propose_failed; endpoints return 503.
    config_draft_maintenance_task = None
    from bowhead_core.config_draft_store import (
        ConfigDraftSchemaUnavailableError,
        ConfigDraftStore,
    )

    from bowhead_gateway.config_draft_maintenance import run_config_draft_maintenance

    config_draft_store = ConfigDraftStore(db_path)
    try:
        await config_draft_store.initialize()
    except ConfigDraftSchemaUnavailableError:
        log.warning("config_draft.runtime_skipped_schema_absent")
    else:
        app.state.config_draft_store = config_draft_store
        config_draft_maintenance_task = asyncio.create_task(
            run_config_draft_maintenance(draft_store=config_draft_store),
            name="config-draft-maintenance",
        )
        app.state.config_draft_maintenance_task = config_draft_maintenance_task
    from bowhead_gateway.edition_extensions import initialize_branding_services

    await initialize_branding_services(app, artifact_store)
    # Public entries are always enabled. First activation creates the initial
    # entry from its trusted request context; startup never seeds one from an
    # environment variable or request host.
    from bowhead_core.public_entries import PublicEntryStore

    public_entry_store = PublicEntryStore(db_path)
    await public_entry_store.initialize()
    app.state.public_entry_store = public_entry_store
    from bowhead_gateway.edition_identity import initialize_identity_services

    await initialize_identity_services(app, Path(db_path))
    # ─── kb-matrix M2: matrix store + execution engine (tech design §7.1) ────
    from bowhead_core.kb.matrix_engine import KbMatrixEngine as _KbMatrixEngine
    from bowhead_core.kb.matrix_store import KbMatrixStore as _KbMatrixStore
    from bowhead_core.kb.structured_extractor import (
        StructuredExtractor as _MatrixStructuredExtractor,
    )

    _kb_matrix_store = _KbMatrixStore(Path(db_path).parent / "kb_matrix.db")
    await _kb_matrix_store.initialize()
    app.state.kb_matrix_store = _kb_matrix_store

    async def _kb_matrix_runtime_resolver(team_id: str):  # type: ignore[no-untyped-def]
        """per-team runtime（§5.4：extractor/router 必须同实例）；失败回 None → 引擎回落默认。"""
        try:
            _cfg, _version = await user_store.get_team_llm_config_with_version(team_id)
            return await _team_kb_runtime_registry.get(team_id, _cfg, _version)
        except Exception:
            logging.getLogger("bowhead_gateway.main").warning("kb_matrix.runtime_resolver_failed team=%s", team_id, exc_info=True)
            return None

    _kb_matrix_engine = _KbMatrixEngine(
        matrix_store=_kb_matrix_store,
        files_store=_kb_files_store,
        kb_store=_kb_store,
        extractor=_MatrixStructuredExtractor(router=orchestrator.router),
        router=orchestrator.router,
        user_store=user_store,
        quotas_store=_kb_workspace_quotas_store,
        runtime_resolver=_kb_matrix_runtime_resolver,
    )
    app.state.kb_matrix_engine = _kb_matrix_engine
    app.state.kb_matrix_run_tasks = _kb_matrix_engine.run_tasks

    # Boot resume (§5.5): fail-soft wrapper (scheduled_store.recover_running_runs pattern);
    # resume atomically claims first to prevent concurrent zombie-scanner writes.
    async def _kb_matrix_boot_resume() -> None:
        try:
            _resumed = await _kb_matrix_engine.resume_interrupted_runs()
            if _resumed:
                logging.getLogger("bowhead_gateway.main").info("kb_matrix boot resume: %d runs", _resumed)
        except Exception:
            logging.getLogger("bowhead_gateway.main").exception("kb_matrix boot resume failed (degraded, not crash)")

    _kb_matrix_zombie_task = asyncio.create_task(_kb_matrix_engine.zombie_scan_loop())
    app.state._kb_matrix_zombie_task = _kb_matrix_zombie_task

    # kb-matrix M4 (FR-40): pending_snapshot self-healing sweep closes the non-atomic two-database crash window
    # (rows present → finish flip; no rows → reclaim/free name). Once at boot, fail-soft.
    async def _kb_matrix_graduation_sweep() -> None:
        try:
            from datetime import UTC as _UTC
            from datetime import datetime as _dt
            from datetime import timedelta as _td

            from bowhead_core.kb.graduation import sweep_pending_graduations as _sweep

            threshold = (_dt.now(tz=_UTC) - _td(hours=1)).isoformat(timespec="seconds")
            counts = await _sweep(_kb_doc_type_store, _kb_extracted_rows_store, older_than_iso=threshold)
            if counts["flipped"] or counts["reclaimed"]:
                logging.getLogger("bowhead_gateway.main").info(
                    "kb_matrix graduation sweep: flipped=%d reclaimed=%d", counts["flipped"], counts["reclaimed"]
                )
        except Exception:
            logging.getLogger("bowhead_gateway.main").exception("kb_matrix graduation sweep failed (degraded)")

    app.state._kb_matrix_graduation_sweep_task = asyncio.create_task(_kb_matrix_graduation_sweep())

    # kb-matrix M4 (FR-41): seed builtin column-group templates per ws (same as catalog_seed:
    # fire-and-forget + marker idempotency).
    async def _kb_template_seed_all_ws() -> None:
        gw_logger = logging.getLogger("bowhead_gateway.main")
        try:
            from bowhead_core.kb.template_seed import seed_templates_for_ws as _seed_tpl

            ws_ids = await _kb_files_store.list_distinct_workspaces()
            for ws_id in ws_ids:
                try:
                    await _seed_tpl(ws_id, _kb_doc_type_store, _kb_marker_store)
                except Exception:
                    gw_logger.exception("template seed failed ws=%r (continuing)", ws_id)
                await asyncio.sleep(0.1)
        except Exception:
            gw_logger.exception("template seed background task failed (degraded)")

    app.state._kb_template_seed_task = asyncio.create_task(_kb_template_seed_all_ws())
    # Batch 2 (workspace-space-split PRD §3.2/§3.5): per-user tool context service; trusted-identity
    # ladder + accessible-set TTL cache. PER_USER OFF means no resolution paths read it (zero behavior).
    from bowhead_gateway.connector_context_service import ConnectorContextService

    app.state.tool_context_service = ConnectorContextService(
        user_store=user_store,
        user_memory_store=memory_store,
    )
    notification_enabled = os.environ.get("BOWHEAD_NOTIFICATION_CENTER", "1").strip() != "0"
    from bowhead_core.approval_gate import ApprovalGate
    from bowhead_core.notification_service import NotificationService

    notification_service = NotificationService(
        store=notification_store,
        user_store=user_store,
        enabled=notification_enabled,
    )
    approval_gate = ApprovalGate(
        approval_store=approval_store,
        notification_service=notification_service,
        enabled=notification_enabled,
    )
    app.state.notification_store = notification_store
    app.state.notification_service = notification_service
    app.state.approval_gate = approval_gate
    app.state.notification_center_enabled = notification_enabled
    orchestrator.notification_service = notification_service
    orchestrator.scheduled_store = scheduled_store  # scheduled_job_create tool reads ctx.scheduled_store
    orchestrator.approval_gate = approval_gate
    if notification_enabled:

        async def _sync_approval_notifications() -> None:
            try:
                reconciled = await notification_service.reconcile_approval_required(approval_store)
                if reconciled:
                    log.info("notification.reconcile_approval_required resolved=%d", reconciled)
                emitted = await notification_service.backfill_pending_approvals(approval_store)
                if emitted:
                    log.info("notification.backfill_pending_approvals emitted=%d", emitted)
            except Exception as exc:
                log.warning("notification.sync_approval_notifications_failed err=%s", exc)

        asyncio.create_task(_sync_approval_notifications())
    from bowhead_core.access_control import AccessControlStore

    access_control_store = AccessControlStore(
        db_path,
        terminal_auth_fence_enabled=(getattr(app.state, "terminal_protocol_store", None) is not None),
    )
    await access_control_store.initialize()
    app.state.access_control_store = access_control_store
    from bowhead_gateway.kb_authorization import kb_file_authorizer

    _kb_authorizer = kb_file_authorizer(user_store, access_control_store)
    from bowhead_gateway.knowledge_management import KnowledgeManagementAccess

    orchestrator.knowledge_management_access = KnowledgeManagementAccess(app)
    orchestrator.kb_file_authorizer = _kb_authorizer
    _kb_matrix_engine._file_authorizer = _kb_authorizer
    _kb_matrix_engine._artifact_store = artifact_store
    app.state.kb_matrix_resume_task = asyncio.create_task(_kb_matrix_boot_resume())

    # content-permissions wave 2: access-request + approval queue.
    from bowhead_core.access_request_store import AccessRequestStore

    access_request_store = AccessRequestStore(db_path)
    await access_request_store.initialize()
    app.state.access_request_store = access_request_store

    from bowhead_gateway.routes.admin_billing import (
        resume_pending_embed_reindexes as _resume_pending_embed_reindexes,
    )

    class _StartupRequest:
        def __init__(self, app_ref: FastAPI) -> None:
            self.app = app_ref
            self.client = None

    resumed_reindexes = await _resume_pending_embed_reindexes(
        _StartupRequest(app),  # type: ignore[arg-type]
        user_store,
    )
    if resumed_reindexes:
        _gw_logger.info("team_kb_reindex.resume_pending count=%d", resumed_reindexes)

    # KB UX remediation FR1: requeue previous-process residue in pending/queued/parsing/chunking/embedding
    # (beyond automatic-recovery cap → failed). Background execution never blocks readiness; requeued files enter via
    # each team's worker.parse_and_index, sharing upload concurrency limits and team parsing gates.
    from bowhead_core.kb.parse_recovery import recover_unfinished_parses as _recover_unfinished_parses

    async def _kb_resolve_parse_worker(team_id: str):  # type: ignore[no-untyped-def]
        if not team_id:
            return None
        _cfg, _version = await user_store.get_team_llm_config_with_version(team_id)
        runtime = await _team_kb_runtime_registry.get(team_id, _cfg, _version)
        return runtime.worker if runtime is not None else None

    async def _kb_recover_unfinished_parses() -> None:
        try:
            recovery = await _recover_unfinished_parses(
                _kb_files_store,
                _kb_resolve_parse_worker,
                started_before=_kb_parse_boot_at,
            )
        except Exception:
            _gw_logger.exception("kb.parse_recovery failed")
            return
        app.state.kb_parse_recovery_tasks = recovery.tasks
        if recovery.requeued or recovery.interrupted or recovery.unavailable:
            _gw_logger.info(
                "kb.parse_recovery requeued=%d interrupted=%d unavailable=%d",
                recovery.requeued,
                recovery.interrupted,
                recovery.unavailable,
            )

    from bowhead_core.kb.maintenance import KbMaintenanceStore

    from bowhead_gateway.kb_maintenance import maintenance_loop

    await KbMaintenanceStore(_kb_files_store._db_path).pause_interrupted()
    _kb_maintenance_task = asyncio.create_task(maintenance_loop(app), name="kb_source_maintenance")
    app.state.kb_parse_recovery_task = asyncio.create_task(
        _kb_recover_unfinished_parses(),
        name="kb_parse_recovery",
    )
    # kb-structured-tables Sprint 2 Phase B: backfill task state (dual-dict per P2-TD-2)
    app.state.kb_backfill_active_ws = {}  # ws_id → task_id (per-ws lock)
    app.state.kb_backfill_tasks_by_id = {}  # task_id → asyncio.Task (cancel lookup)
    app.state.kb_backfill_status = {}  # task_id → BackfillStatus DTO
    # turn_runner picks via getattr(orch, "kb_retriever", None) → ToolExecutionContext
    orchestrator.kb_retriever = _kb_retriever  # type: ignore[attr-defined]
    # V7 kb-agentic-fix §2.5: wire kb_files_store to _handle_kb_search for ws KB summaries.
    orchestrator.kb_files_store = _kb_files_store  # type: ignore[attr-defined]
    # kb-knowledge-tables sprint v1.0: doc_type + record stores for kb_list_tables /
    # kb_query_records / kb_get_record tools (turn_runner picks via getattr).
    orchestrator.kb_doc_type_store = _kb_doc_type_store  # type: ignore[attr-defined]
    orchestrator.kb_extracted_record_store = _kb_extracted_record_store  # type: ignore[attr-defined]
    # Sprint 25 kb-per-row-extract: row-level store for chat dispatch
    orchestrator.kb_extracted_rows_store = _kb_extracted_rows_store  # type: ignore[attr-defined]

    # canvas_store cascade invalidation hook (post-commit, sync await).
    # Hook only runs effective work when ANCHOR_INVALIDATION_ENABLED=1.
    async def _anchor_invalidation_hook(event_kind, canvas_id, section_id, new_payload):
        if not getattr(app.state, "anchor_invalidation_enabled", False):
            return
        n = 0
        try:
            if event_kind == "canvas_deleted":
                n = await memory_note_anchors_store.invalidate_by_canvas(
                    canvas_id=canvas_id,
                    reason="canvas_deleted",
                )
            elif event_kind == "section_deleted":
                n = await memory_note_anchors_store.invalidate_by_section(
                    canvas_id=canvas_id,
                    section_id=section_id,
                    reason="section_deleted",
                )
            elif event_kind == "payload_changed":
                n = await memory_note_anchors_store.recheck_hash(
                    canvas_id=canvas_id,
                    section_id=section_id,
                    new_payload=new_payload or {},
                )
        except Exception:
            log.warning("anchor_invalidation.cascade_failed", event=event_kind, canvas_id=canvas_id, section_id=section_id)
            return
        if n > 0:
            with suppress(Exception):
                await user_store.log_audit(
                    user_id=None,
                    team_id=None,
                    event_type=f"memory.anchor.invalidated_by_{event_kind.split('_')[0]}",
                    payload={
                        "canvas_id": canvas_id,
                        "section_id": section_id,
                        "affected_count": n,
                    },
                )

    canvas_store._anchor_invalidation_hook = _anchor_invalidation_hook

    # i18n v1: workspace settings store (data_language / week_start_day / i18n_v1 flag)
    from bowhead_core.workspace_settings_store import WorkspaceSettingsStore

    workspace_settings_store = WorkspaceSettingsStore(db_path)
    await workspace_settings_store.init()
    app.state.workspace_settings_store = workspace_settings_store

    # P4.5.1: User / Team / credit / audit store — MOVED earlier (must precede its first
    # use in _resume_pending_embed_reindexes). See the [0.51.0 merge-fix] block above.

    from bowhead_core.entitlements import deployment_mode_from_env
    from bowhead_core.installation import initialize_installation

    from bowhead_gateway.edition_extensions import initialize_entitlement_services

    _deployment_mode = deployment_mode_from_env()
    app.state.deployment_mode = _deployment_mode
    await initialize_installation(db_path, _build_edition, admitted_fresh=_fresh_installation)
    await initialize_entitlement_services(app, user_store, _deployment_mode)
    _enforce = app.state.entitlements.enforce
    # Every account creation uses the composed seat policy. Community has no seat ceiling;
    # Business retains its existing enforcement and shadow configuration.
    from bowhead_gateway._entitlement_gate import feature_license_checker

    orchestrator.personal_mcp_license_check = feature_license_checker(app, "mcp")
    user_store.set_seat_guard(app.state.entitlements)
    log.info("entitlement.resolver_ready", mode=_deployment_mode, enforce=_enforce)
    if _deployment_mode == "local":
        try:
            openapi_log_retention_days = max(1, int(os.environ.get("BOWHEAD_OPENAPI_LOG_RETENTION_DAYS", "30")))
        except ValueError:
            openapi_log_retention_days = 30

        async def _prune_openapi_logs() -> None:
            while True:
                try:
                    await app.state.openapi_store.prune_logs(openapi_log_retention_days)
                except Exception:
                    log.exception("team_openapi.log_prune_failed")
                await asyncio.sleep(24 * 60 * 60)

        openapi_log_prune_task = asyncio.create_task(_prune_openapi_logs(), name="team-openapi-log-prune")

    # Sprint α G3 Phase 2: SMTP outbox worker (single asyncio task per Q3 LOCKED)
    import asyncio as _asyncio_smtp

    from bowhead_gateway.workers.smtp_outbox import smtp_outbox_worker

    app.state._smtp_outbox_task = _asyncio_smtp.create_task(
        smtp_outbox_worker(user_store, poll_interval=30.0),
    )
    # P4.5.2: per-team Router cache — MOVED above (must precede TeamKbRuntimeRegistry, which reads
    # app.state.team_router_registry). See the [0.51.0 merge-fix] block near the KB-registry setup.
    # cca ④d/LOAD POINT: per-(team,source) decrypted tool-auth header cache (mirror
    # TeamRouterRegistry). The LOAD POINT executor's source-resolver calls get_headers
    # to inject per-team Fernet-decrypted auth on each outbound team-tool call.
    from bowhead_core.tool_auth_registry import ToolAuthRegistry

    tool_auth_registry = ToolAuthRegistry(user_store)
    await tool_auth_registry.start_background()
    app.state.tool_auth_registry = tool_auth_registry
    # LOAD POINT wiring: hand the team-tool deps to the orchestrator so turn_runner can
    # build the per-turn team tool view (declared fields → carried through the BYO-LLM
    # dataclasses.replace at chat.py). Both already None-default ⇒ native-only otherwise.
    orchestrator.user_store = user_store
    orchestrator.access_control_store = access_control_store
    orchestrator.tool_auth_registry = tool_auth_registry
    # Builtin subagent management: team deltas for system builtin subagents. turn_runner fetches once per turn
    # per instance. Attach unconditionally; one read-side flag gate prevents wired-but-unread/read-but-unwired mismatches.
    from bowhead_core.builtin_subagent_override_store import (
        BuiltinSubagentOverrideStore,
    )

    _builtin_override_store = BuiltinSubagentOverrideStore(db_path)
    orchestrator.builtin_subagent_override_store = _builtin_override_store  # type: ignore[attr-defined]
    # Agent run limits are read once per turn from the Team settings (turn_runner._run_limits_prefetch).
    orchestrator.team_settings_store = team_settings_store  # type: ignore[attr-defined]
    app.state.builtin_subagent_override_store = _builtin_override_store
    # cca P3 (dogfood fix): the boot-time subagent_runner singleton (built ~L825, BEFORE
    # user_store existed) had team_subagent_store=None, and turn_runner REUSES that singleton for
    # spawn EXECUTION — so team subagents were ADVERTISED to the Lead but FAILED to RESOLVE on
    # spawn ("unknown profile: <name>"). Wire the store onto the singleton now that user_store
    # exists. The Creator switch controls authoring only; existing definitions remain runtime
    # resolvable, subject to assignment and canonical access admission.
    # Safe on the reusable singleton: the store is invariant across turns; team_id (from parent_ctx)
    # scopes every lookup, so there is no cross-team leak.
    if getattr(orchestrator, "subagent_runner", None) is not None:
        orchestrator.subagent_runner.team_subagent_store = user_store  # type: ignore[attr-defined]
    # P4.5.13: GDPR purge + monthly credit reset cron
    import asyncio as _asyncio

    from bowhead_core.user_cron import run_cron_loop

    app.state.user_cron_task = _asyncio.create_task(run_cron_loop(db_path))
    from bowhead_gateway.credit_attention_worker import (
        env_materializer_enabled as _credit_attention_enabled,
    )

    if _credit_attention_enabled():
        from bowhead_gateway.credit_attention_worker import (
            env_interval_s as _credit_attention_interval_s,
        )
        from bowhead_gateway.credit_attention_worker import (
            run_credit_attention_materializer,
        )

        app.state.credit_attention_materializer_task = _asyncio.create_task(
            run_credit_attention_materializer(
                user_store,
                interval_s=_credit_attention_interval_s(),
                metrics_log=metrics_log,
            ),
        )

    # V3 PRD-B: memory GC daily cron (singleton lock via memory_gc_state UPDATE rowcount).
    # Tasks: trash >30d hard-purge, candidates >30d auto-reject, removed-user
    # cascade purge, reset_snapshots >30d, metrics_event_log >90d.
    from bowhead_core.memory_gc import memory_gc_loop

    _memory_gc_task = _asyncio.create_task(
        memory_gc_loop(
            db_path=db_path,
            user_memory_store=memory_store,
            candidate_store=memory_candidate_store,
            metrics_log=metrics_log,
            notes_store=memory_notes_store,  # V3.5 PRD-C
            snapshots_store=memory_pack_snapshots_store,  # V4 PRD-D-4-4
            anchors_store=memory_note_anchors_store,  # V4 PRD-D-4-1
            notification_service=notification_service,  # review-fix P1: badge resolve
            destructive_operation_store=(memory_destructive_operation_store),
        ),
    )
    app.state.tool_registry = tool_registry
    app.state.skill_instance_store = skill_instance_store
    app.state.memory_candidate_store = memory_candidate_store
    app.state.personal_memory_governance_store = personal_memory_governance_store
    app.state.dreaming_light = dreaming_light
    app.state.vector_client = vector_client
    app.state.memory_destructive_operation_store = memory_destructive_operation_store
    app.state.privacy_deletion_ledger = privacy_deletion_ledger
    from bowhead_core.account_deletion_service import AccountDeletionService

    from bowhead_gateway.account_deletion_cleanup import AccountExternalCleanup

    app.state.account_deletion_service = None
    app.state.account_deletion_task = None
    if account_deletion_ledger is not None and app.state.session_lifecycle_coordinator is not None:
        account_external_cleanup = AccountExternalCleanup(app)
        app.state.account_deletion_service = AccountDeletionService(
            user_store,
            lifecycle=app.state.session_lifecycle_coordinator,
            artifacts=artifact_store,
            ledger=account_deletion_ledger,
            secret=_privacy_ledger_key,
            external_cleanup=account_external_cleanup,
            external_quarantine=account_external_cleanup.quarantine,
            erasure_guard=account_external_cleanup.guard,
        )
        app.state.account_deletion_task = asyncio.create_task(app.state.account_deletion_service.run(), name="account-deletion")
    app.state.schema_cache = schema_cache
    app.state.clarification_store = clarification_store
    app.state.routing_mode = mode
    app.state.routing_config_loaded = loaded_cfg

    # Clarification invalidation is intentionally not a best-effort feature.
    # All participating stores must identify the same SQLite file, Alembic
    # 0017 must be present, and the durable Turn sink must already be installed
    # before recovery can run or this instance may advertise readiness.
    # Shared-database and 0017 gates were already checked immediately after ClarificationStore initialization.
    from bowhead_gateway.clarification_invalidation import (
        STARTUP_BATCH_LIMIT,
        clarification_invalidation_publisher_loop,
        publish_pending_clarification_invalidations,
        require_durable_event_sink,
    )

    require_durable_event_sink()
    _clarification_invalidation_startup_delivered = await publish_pending_clarification_invalidations(
        app,
        limit=STARTUP_BATCH_LIMIT,
    )
    if _clarification_invalidation_startup_delivered:
        log.info(
            "clarification_invalidation.startup_recovered",
            count=_clarification_invalidation_startup_delivered,
        )
    _clarification_invalidation_publisher_task = asyncio.create_task(
        clarification_invalidation_publisher_loop(app),
        name="clarification-invalidation-publisher",
    )

    # The caller team's resolved client is supplied for each lookup; no global
    # Datarc connection or deployment identity is retained in app state.
    from bowhead_gateway.member_resolver import MemberUidResolver

    app.state.member_resolver = MemberUidResolver()

    # S5 production assembly.  One process-unique writer capability owns both
    # live runtimes and recovery; OFF means draining/recovery-only, never a
    # legacy fallback for persisted v1 Turns.
    if app.state.terminal_protocol_store is not None:
        from bowhead_core.terminal_generation_bus import (
            GenerationEventBusRegistry,
        )
        from bowhead_core.terminal_protocol import canonical_digest_v1
        from bowhead_core.terminal_reconciler import (
            TerminalRecoveryProcessor,
            TerminalRecoverySweepResult,
            continue_bounded_terminal_recovery,
        )
        from bowhead_core.terminal_runtime_factory import (
            TerminalRuntimeFactory,
            TerminalRuntimeRegistry,
            TerminalWriterSnapshot,
        )

        from bowhead_gateway.terminal_approval_runner import (
            TerminalApprovalRecovery,
            TerminalApprovalRunner,
        )
        from bowhead_gateway.terminal_decision_authorization import (
            TerminalDecisionAuthorizer,
            TerminalPendingControlAuthorization,
            TerminalPendingDecisionRevocation,
        )
        from bowhead_gateway.terminal_decision_effects import (
            TerminalClarificationEffectRecovery,
        )
        from bowhead_gateway.terminal_pending_execution import (
            TerminalPendingExecutionRecovery,
            resolve_terminal_followup_request,
        )
        from bowhead_gateway.terminal_writer_service import (
            TerminalWriterLeaseService,
        )

        async def _read_terminal_recovery_inventory_snapshot() -> tuple[
            int,
            str | None,
            str | None,
            tuple[str, ...],
        ]:
            errors: list[str] = []
            try:
                remaining = await app.state.terminal_protocol_store.count_recovery_inventory()
            except Exception:
                remaining = 0
                errors.append("terminal_recovery_inventory_unavailable")
            try:
                next_due = await app.state.terminal_protocol_store.read_recovery_next_due_at()
            except Exception:
                next_due = None
                errors.append("terminal_recovery_next_due_unavailable")
            try:
                max_due = await app.state.terminal_protocol_store.read_recovery_max_due_at()
            except Exception:
                max_due = None
                errors.append("terminal_recovery_max_due_unavailable")
            return remaining, next_due, max_due, tuple(errors)

        _terminal_recovery_owner = f"gateway-terminal:{os.getpid()}:{uuid4().hex}"
        _terminal_rollout_config = durable_chat_execution_config()
        _terminal_rollout_config_generation = _terminal_rollout_config.config_generation
        _terminal_fresh_writer_enabled = fresh_terminal_v1_admission_enabled(
            _terminal_rollout_config,
        )
        terminal_runtime_registry = TerminalRuntimeRegistry()
        _terminal_writer_service = TerminalWriterLeaseService(
            store=app.state.terminal_protocol_store,
            owner_instance_id=_terminal_recovery_owner,
            rollout_config_generation=_terminal_rollout_config_generation,
            fresh_admission_enabled=_terminal_fresh_writer_enabled,
            active_runtime_count=lambda: terminal_runtime_registry.size,
        )
        (
            _terminal_initial_inventory,
            _terminal_initial_next_due,
            _terminal_initial_max_due,
            _terminal_initial_inventory_errors,
        ) = await _read_terminal_recovery_inventory_snapshot()
        # OFF briefly acquires a recovery fence so the on-demand factory can
        # be assembled, then releases it below when inventory/workers are idle.
        try:
            writer_acquired = await _terminal_writer_service.start()
        except Exception as exc:
            writer_acquired = False
            log.warning(
                "terminal_writer.startup_acquire_failed",
                error=type(exc).__name__,
            )
        app.state.terminal_writer_service = _terminal_writer_service
        app.state.terminal_runtime_factory = None
        app.state.terminal_approval_runner = None
        if app.state.terminal_protocol_store is not None:
            _terminal_recovery_processor: TerminalRecoveryProcessor | None = None
            _terminal_recovery_run_lock = asyncio.Lock()
            _terminal_recovery_triggers_enabled = True

            def _terminal_recovery_converged(
                result: TerminalRecoverySweepResult,
            ) -> bool:
                return bool(result.eligible_remaining == 0 and result.next_due_at is None and result.max_due_at is None and not result.errors)

            def _publish_terminal_recovery_result(
                result: TerminalRecoverySweepResult,
            ) -> None:
                app.state.terminal_recovery_sweep = result
                if result.errors:
                    _terminal_writer_service.mark_recovery_pending()
                elif _terminal_recovery_converged(result):
                    _terminal_writer_service.mark_recovery_ready()

            def _trigger_terminal_recovery(
                _scope: Any,
                _reason: str,
            ) -> None:
                if not _terminal_recovery_triggers_enabled:
                    return
                processor = _terminal_recovery_processor
                if processor is None:
                    return

                async def _run_triggered_recovery() -> None:
                    if _reason == "decision_recorded":
                        # Give the initiating HTTP flow first chance to attach;
                        # ack loss/page close still converges automatically.
                        await asyncio.sleep(1)
                    from datetime import UTC, datetime, timedelta

                    deadline = datetime.now(tz=UTC) + timedelta(seconds=60)
                    try:
                        async with _terminal_recovery_run_lock:
                            while True:
                                try:
                                    if not await _terminal_writer_service.start():
                                        raise RuntimeError("terminal_writer_lease_unavailable")
                                    fence = _terminal_writer_service.fence
                                    if fence is None:
                                        raise RuntimeError("terminal_writer_lease_unavailable")
                                    initial = await processor.run_bounded(
                                        owner_instance_id=(_terminal_recovery_owner),
                                        rollout_config_generation=(_terminal_rollout_config_generation),
                                        writer_fence=fence,
                                    )
                                    recovered = await continue_bounded_terminal_recovery(
                                        processor,
                                        initial,
                                        owner_instance_id=(_terminal_recovery_owner),
                                        rollout_config_generation=(_terminal_rollout_config_generation),
                                        writer_fence=fence,
                                    )
                                    _publish_terminal_recovery_result(recovered)
                                    return
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:
                                    (
                                        remaining,
                                        next_due,
                                        max_due,
                                        inventory_errors,
                                    ) = await _read_terminal_recovery_inventory_snapshot()
                                    code = (f"terminal_recovery_trigger_{type(exc).__name__.lower()}")[:128]
                                    failed = TerminalRecoverySweepResult(
                                        processed_count=0,
                                        eligible_remaining=remaining,
                                        pending_digest=canonical_digest_v1(
                                            {
                                                "reason": _reason,
                                                "error": code,
                                                "remaining": remaining,
                                                "next_due_at": next_due,
                                                "max_due_at": max_due,
                                            }
                                        ),
                                        fence_acquired=False,
                                        next_due_at=next_due,
                                        max_due_at=max_due,
                                        page_cursor=None,
                                        errors=(code, *inventory_errors),
                                    )
                                    _publish_terminal_recovery_result(failed)
                                    log.warning(
                                        "terminal_recovery.trigger_failed",
                                        reason=_reason,
                                        error=type(exc).__name__,
                                    )
                                    now = datetime.now(tz=UTC)
                                    if now >= deadline:
                                        return
                                    delay = 1.0
                                    if next_due:
                                        due = datetime.fromisoformat(next_due.replace("Z", "+00:00"))
                                        delay = max(
                                            0.1,
                                            (due - now).total_seconds() + 2,
                                        )
                                    await asyncio.sleep(
                                        min(
                                            delay,
                                            max(
                                                0.1,
                                                (deadline - now).total_seconds(),
                                            ),
                                            5.0,
                                        )
                                    )
                    finally:
                        if not _terminal_fresh_writer_enabled:
                            await _terminal_writer_service.release_recovery_if_idle()

                task = asyncio.create_task(
                    _run_triggered_recovery(),
                    name=f"terminal-recovery-trigger:{_reason}",
                )
                _terminal_recovery_tasks.add(task)
                task.add_done_callback(_terminal_recovery_tasks.discard)

            terminal_generation_buses = GenerationEventBusRegistry()
            terminal_factory = TerminalRuntimeFactory(
                app.state.terminal_protocol_store,
                terminal_generation_buses,
                terminal_runtime_registry,
                (
                    _terminal_writer_service.writer_snapshot()
                    if writer_acquired
                    else TerminalWriterSnapshot(
                        _terminal_recovery_owner,
                        1,
                        1,
                        _terminal_rollout_config_generation,
                        recovery_only=(not _terminal_fresh_writer_enabled),
                    )
                ),
                recovery_trigger=_trigger_terminal_recovery,
                writer_snapshot_provider=(_terminal_writer_service.writer_snapshot),
                worker_task_observer=(_terminal_writer_service.observe_worker_task),
                writer_ready=_terminal_writer_service.ensure_writer_ready,
                fresh_admission_ready=(lambda: _terminal_writer_service.fresh_admission_ready),
                writer_idle_release=(_terminal_writer_service.release_recovery_if_idle if not _terminal_fresh_writer_enabled else None),
                followup_request_resolver=(
                    lambda template, parent_scope: resolve_terminal_followup_request(
                        app,
                        template,
                        parent_scope,
                    )
                ),
            )
            terminal_approval_runner = TerminalApprovalRunner(
                factory=terminal_factory,
                approval_store=approval_store,
                authorizer=TerminalDecisionAuthorizer(app=app),
            )
            _terminal_recovery_processor = TerminalRecoveryProcessor(
                app.state.terminal_protocol_store,
                pending_execution_recoverer=(
                    TerminalPendingExecutionRecovery(
                        app=app,
                        factory=terminal_factory,
                        protocol_store=app.state.terminal_protocol_store,
                        approval_runner=terminal_approval_runner,
                        authorizer=TerminalDecisionAuthorizer(app=app),
                    )
                ),
                settled_effect_recoverer=TerminalApprovalRecovery(
                    protocol_store=app.state.terminal_protocol_store,
                    approval_store=approval_store,
                ),
                decision_effect_recoverer=(
                    TerminalClarificationEffectRecovery(
                        app=app,
                        protocol_store=app.state.terminal_protocol_store,
                    )
                ),
                pending_decision_revoker=(
                    TerminalPendingDecisionRevocation(
                        protocol_store=(app.state.terminal_protocol_store),
                        factory=terminal_factory,
                    )
                ),
                pending_control_authorizer=(TerminalPendingControlAuthorization(app=app)),
            )
            app.state.terminal_runtime_factory = terminal_factory
            app.state.terminal_generation_bus_registry = terminal_generation_buses
            app.state.terminal_runtime_registry = terminal_runtime_registry
            app.state.terminal_approval_runner = terminal_approval_runner
            app.state.terminal_recovery_processor = _terminal_recovery_processor
            try:
                if _terminal_initial_inventory_errors:
                    raise RuntimeError("terminal_recovery_inventory_unavailable")
                if not writer_acquired:
                    raise RuntimeError("terminal_writer_lease_unavailable")
                _terminal_recovery_initial = await _terminal_recovery_processor.run_bounded(
                    owner_instance_id=_terminal_recovery_owner,
                    rollout_config_generation=(_terminal_rollout_config_generation),
                    writer_fence=_terminal_writer_service.fence,
                )
            except Exception as exc:
                log.warning(
                    "terminal_recovery.startup_immediate_failed",
                    error=type(exc).__name__,
                )
                (
                    current_inventory,
                    current_next_due,
                    current_max_due,
                    inventory_errors,
                ) = await _read_terminal_recovery_inventory_snapshot()
                _terminal_recovery_initial = TerminalRecoverySweepResult(
                    processed_count=0,
                    eligible_remaining=current_inventory,
                    pending_digest=("terminal_recovery_startup_retry_required"),
                    fence_acquired=False,
                    next_due_at=current_next_due,
                    max_due_at=current_max_due,
                    page_cursor=None,
                    errors=("terminal_recovery_startup_failed", *inventory_errors),
                )
        else:
            _terminal_recovery_initial = TerminalRecoverySweepResult(
                processed_count=0,
                eligible_remaining=_terminal_initial_inventory,
                pending_digest="terminal_writer_lease_unavailable",
                fence_acquired=False,
                next_due_at=None,
                page_cursor=None,
                errors=("terminal_writer_lease_unavailable",),
            )
        _publish_terminal_recovery_result(_terminal_recovery_initial)
        if writer_acquired and not _terminal_fresh_writer_enabled:
            await _terminal_writer_service.release_recovery_if_idle()

        async def _terminal_recovery_monitor() -> None:
            while True:
                try:
                    inventory = await app.state.terminal_protocol_store.count_recovery_inventory()
                    if not _terminal_fresh_writer_enabled and inventory == 0:
                        await _terminal_writer_service.release_recovery_if_idle()
                    else:
                        async with _terminal_recovery_run_lock:
                            if not await _terminal_writer_service.start():
                                _terminal_writer_service.mark_recovery_pending()
                                raise RuntimeError("terminal_writer_lease_unavailable")
                            fence = _terminal_writer_service.fence
                            if fence is None:
                                _terminal_writer_service.mark_recovery_pending()
                                raise RuntimeError("terminal_writer_lease_unavailable")
                            result = await _terminal_recovery_processor.run_bounded(
                                owner_instance_id=_terminal_recovery_owner,
                                rollout_config_generation=(_terminal_rollout_config_generation),
                                writer_fence=fence,
                            )
                            _publish_terminal_recovery_result(result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _terminal_writer_service.mark_recovery_pending()
                    (
                        remaining,
                        next_due,
                        max_due,
                        inventory_errors,
                    ) = await _read_terminal_recovery_inventory_snapshot()
                    code = (f"terminal_recovery_monitor_{type(exc).__name__.lower()}")[:128]
                    failed = TerminalRecoverySweepResult(
                        processed_count=0,
                        eligible_remaining=remaining,
                        pending_digest=canonical_digest_v1(
                            {
                                "error": code,
                                "remaining": remaining,
                                "next_due_at": next_due,
                                "max_due_at": max_due,
                            }
                        ),
                        fence_acquired=False,
                        next_due_at=next_due,
                        max_due_at=max_due,
                        page_cursor=None,
                        errors=(
                            "terminal_recovery_monitor_failed",
                            *inventory_errors,
                        ),
                    )
                    _publish_terminal_recovery_result(failed)
                    log.warning(
                        "terminal_recovery.monitor_failed",
                        error=type(exc).__name__,
                    )
                finally:
                    if not _terminal_fresh_writer_enabled:
                        with suppress(Exception):
                            await _terminal_writer_service.release_recovery_if_idle()
                await asyncio.sleep(5)

        _terminal_recovery_monitor_task = asyncio.create_task(
            _terminal_recovery_monitor(),
            name="terminal-recovery-monitor",
        )

        async def _terminal_auth_revoke_loop() -> None:
            from bowhead_gateway.terminal_control_coordinator import (
                revoke_inaccessible_terminal_decisions,
            )

            while True:
                try:
                    await asyncio.sleep(15)
                    revoked = await revoke_inaccessible_terminal_decisions(app)
                    if revoked:
                        log.info(
                            "terminal_authorization.revoked",
                            count=revoked,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "terminal_authorization.revoke_loop_failed",
                        error=type(exc).__name__,
                    )

        _terminal_auth_revoke_task = asyncio.create_task(
            _terminal_auth_revoke_loop(),
            name="terminal-authorization-revoke",
        )

    feishu_long_connection_worker = getattr(app.state, "feishu_long_connection_worker", None)
    if feishu_long_connection_worker is not None:
        feishu_long_connection_worker.start()

    from bowhead_core.export_retention import run_export_retention

    try:
        export_retention_interval = int(
            os.environ.get(
                "BOWHEAD_EXPORT_RETENTION_INTERVAL_SECONDS",
                "86400",
            )
        )
    except ValueError:
        export_retention_interval = 86400
    export_retention_interval = max(
        3600,
        min(export_retention_interval, 7 * 86400),
    )
    try:
        export_staging_retention_hours = int(
            os.environ.get(
                "BOWHEAD_EXPORT_STAGING_RETENTION_HOURS",
                "24",
            )
        )
    except ValueError:
        export_staging_retention_hours = 24
    export_staging_retention_hours = max(
        1,
        min(export_staging_retention_hours, 24 * 30),
    )
    export_staging_root = Path(
        os.environ.get(
            "BOWHEAD_EXPORT_STAGING_ROOT",
            "/tmp/bowhead-exports",
        )
    )

    async def _export_retention_loop() -> None:
        await asyncio.sleep(60)
        while True:
            try:
                result = await run_export_retention(
                    store=export_store,
                    staging_root=export_staging_root,
                    staging_retention_hours=(export_staging_retention_hours),
                )
                if result.jobs_deleted or result.snapshots_deleted or result.staging_job_dirs_deleted or result.staging_dirs_skipped:
                    log.info(
                        "canvas_export.retention_done",
                        jobs_deleted=result.jobs_deleted,
                        snapshots_deleted=result.snapshots_deleted,
                        staging_job_dirs_deleted=(result.staging_job_dirs_deleted),
                        staging_bytes_deleted=(result.staging_bytes_deleted),
                        staging_dirs_skipped=(result.staging_dirs_skipped),
                    )
                await asyncio.sleep(export_retention_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "canvas_export.retention_failed",
                    error=type(exc).__name__,
                )
                await asyncio.sleep(3600)

    export_retention_task: asyncio.Task[None] | None = None

    # If no processors registered successfully (image lacks node/pdftoppm/soffice, or unit tests), skip polling;
    # runner repeatedly claims/reclaims export queues, wasting DB writes with no work.
    if export_processor_registry.supported_pairs():
        await export_runner.start()
        log.info("canvas_export.runner_started", worker_id=export_runner.worker_id)

    # Batch 0 0-A (chart-table-ux tech-design §1.1): V2 export capability assembly results
    # must appear in startup logs. Any of three assembly failures (store / runner / processor)
    # silently returns empty capability lists → FE silently falls back to legacy → actual production
    # continually exports PDFs with charts replaced by floating-point tables, without diagnostic clues (cv_55d680eda3f7 observed).
    _export_v2_caps: list[str] = []
    _export_v2_disabled: list[str] = []
    from bowhead_gateway.routes.canvas import _EXPORT_PARITY_CONTRACTS

    for _fmt, _profile in _EXPORT_PARITY_CONTRACTS:
        if not export_runner.is_available_for(_fmt, _profile):
            _export_v2_disabled.append(f"processor:{_fmt}/{_profile}")
        else:
            _export_v2_caps.append(f"{_fmt}/{_profile}")
    log.info(
        "canvas_export.v2_capabilities",
        available=_export_v2_caps,
        disabled=_export_v2_disabled,
    )

    log.info("gateway.ready", routing_mode=mode, providers=provider_registry.list_names())

    # Routines: any 'running' run is an orphan after a restart (in-process scheduler + run-now keep no
    # run across a gateway restart). Recover them so run-now's has_active_run mutex isn't blocked forever.
    # Runs regardless of whether the cron scheduler is enabled (run-now works with the scheduler off).
    try:
        _recovered = await scheduled_store.recover_running_runs()
        if _recovered:
            log.info("scheduler.recovered_orphan_runs n=%d", _recovered)
    except Exception as exc:
        log.warning("scheduler.recover_orphan_runs_failed err=%s", exc)

    # #126 scheduled worker (in-process, default OFF; enable BOWHEAD_SCHEDULER_ENABLED=1).
    # The dispatch closure is extracted to bowhead_gateway._scheduled_runner.run_scheduled_turn so the entitlement
    # gate (§3.3③) is module-level + unit-testable; bound to `app` via lambda for the RunCallback(job) shape.
    scheduler = None
    from bowhead_core.scheduler_loop import SchedulerLoop, is_scheduler_enabled

    if is_scheduler_enabled():
        from bowhead_gateway._scheduled_runner import (
            admit_scheduled_execution,
            is_scheduled_resume_in_flight,
            reconcile_awaiting_runs,
            run_scheduled_turn,
        )

        scheduler = SchedulerLoop(
            scheduled_store,
            lambda job: run_scheduled_turn(app, job),
            notify=notification_service,
            admit_cb=lambda job: admit_scheduled_execution(app, job),
            # S3 suspension pair: reconcile answered suspensions / detect currently resuming ones (do not race
            # to expire them). Core stores already expire runs/clarifications in one transaction.
            reconcile_cb=lambda: reconcile_awaiting_runs(app),
            resume_probe=lambda turn_id: is_scheduled_resume_in_flight(app, turn_id),
        )
        # At boot, initially calculate next_run_at for all active jobs currently with next_run_at=NULL.
        for _j in await scheduled_store.list_for_user(user_id=""):  # No-op; recalculate across all users.
            pass
        await scheduler.start()
        app.state.scheduler_loop = scheduler
        log.info("scheduler_loop.enabled")

    # Sprint file-upload-presign Phase 1.5: orphan sweeper background task.
    # Disabled with BOWHEAD_PRESIGN_SWEEPER_DISABLED=1 (tests + emergency).
    _presign_sweeper_task: asyncio.Task | None = None
    if os.environ.get("BOWHEAD_PRESIGN_SWEEPER_DISABLED") != "1":
        from bowhead_gateway.presign_sweeper import (
            env_interval_s,
            run_presign_sweeper,
        )

        _presign_sweeper_task = asyncio.create_task(
            run_presign_sweeper(
                artifact_store=artifact_store,
                kb_files_store=_kb_files_store,
                interval_s=env_interval_s(),
            ),
            name="presign_sweeper",
        )
        log.info("presign_sweeper.enabled")

    # Notification Center archive-retention sweeper.
    # Disabled with BOWHEAD_NOTIFICATION_SWEEPER_DISABLED=1 (tests + emergency).
    _notification_sweeper_task: asyncio.Task | None = None
    if notification_enabled and os.environ.get("BOWHEAD_NOTIFICATION_SWEEPER_DISABLED") != "1":
        from bowhead_gateway.notification_sweeper import (
            env_interval_s as notification_sweeper_interval_s,
        )
        from bowhead_gateway.notification_sweeper import (
            env_retention_days,
            run_notification_sweeper,
        )

        _notification_sweeper_task = asyncio.create_task(
            run_notification_sweeper(
                store=notification_store,
                notification_service=notification_service,
                approval_store=approval_store,
                interval_s=notification_sweeper_interval_s(),
                retention_days=env_retention_days(),
            ),
            name="notification_sweeper",
        )
        log.info("notification_sweeper.enabled")

    # Access-request lifecycle sweeper (expiry + 72h escalation).
    _access_request_sweeper_task: asyncio.Task | None = None
    from bowhead_core.access_request_flags import access_requests_enabled as _acr_enabled

    from bowhead_gateway.access_request_sweeper import run_access_request_sweeper, sweeper_disabled

    if _acr_enabled() and not sweeper_disabled():
        _access_request_sweeper_task = asyncio.create_task(
            run_access_request_sweeper(app),
            name="access_request_sweeper",
        )

    # agent-feedback-loop batch 3a: coach casework worker (receipt expiration / background drafting / fatigue gate / explicit 402).
    _feedback_casework_task: asyncio.Task | None = None
    if os.environ.get("BOWHEAD_FEEDBACK_CASEWORK_DISABLED") != "1":
        from bowhead_gateway.feedback_casework_worker import run_feedback_casework_worker

        _feedback_casework_task = asyncio.create_task(
            run_feedback_casework_worker(
                app.state,
                candidate_store=app.state.feedback_candidate_store,
                casework_store=app.state.feedback_casework_store,
                user_store=user_store,
                session_store=session_store,
                notification_service=getattr(app.state, "notification_service", None),
                team_settings_store=getattr(app.state, "team_settings_store", None),
            ),
            name="feedback_casework",
        )

    # Agent release notifications are committed with the release and projected
    # asynchronously into the existing Notification Center. There is no
    # separate product flag: when Notification Center is enabled, the durable
    # outbox is always drained.
    _agent_release_outbox_task: asyncio.Task | None = None
    if notification_enabled:
        from uuid import uuid4 as _uuid4_agent_release

        from bowhead_core.agent_release_outbox import (
            AgentReleaseNotificationOutbox,
        )

        from bowhead_gateway.agent_release_outbox_worker import (
            run_agent_release_outbox_worker,
        )

        agent_release_outbox = AgentReleaseNotificationOutbox(
            db_path,
            notification_store=notification_store,
        )
        app.state.agent_release_outbox = agent_release_outbox
        _agent_release_outbox_task = asyncio.create_task(
            run_agent_release_outbox_worker(
                outbox=agent_release_outbox,
                worker_id=f"gateway:{_uuid4_agent_release().hex}",
            ),
            name="agent-release-outbox",
        )
        log.info("agent_release_outbox.enabled")

    # Agent evaluation is part of the standard release workflow.
    from bowhead_gateway.agent_eval_runtime import (
        AgentEvalRuntime,
        run_agent_eval_worker,
    )

    _agent_eval_runtime = AgentEvalRuntime(
        db_path=db_path,
        state=app.state,
        notification_store=(notification_store if notification_enabled else None),
    )
    app.state.agent_eval_runtime = _agent_eval_runtime
    _agent_eval_task = asyncio.create_task(
        run_agent_eval_worker(_agent_eval_runtime),
        name="agent-eval-worker",
    )
    log.info("agent_eval.enabled")

    # credit-billing gap 7: LLM usage recorder; register bowhead_router's metering sink, buffering each call's
    # usage in memory, then batch-persisting llm_usage_events. **Meter only, no billing** (balances still use atomic
    # turn-finalization debits), exposing formerly unaccounted background calls (dreaming memory candidates / workflow
    # distillation / session titles). BOWHEAD_LLM_USAGE_RECORDER_DISABLED=1 disables registration; no sink means
    # the router's entire path is a no-op.
    _llm_usage_recorder = None
    if os.environ.get("BOWHEAD_LLM_USAGE_RECORDER_DISABLED") != "1":
        from bowhead_gateway.llm_usage_recorder import start_llm_usage_recorder

        _llm_usage_recorder = start_llm_usage_recorder(user_store)
        app.state.llm_usage_recorder = _llm_usage_recorder

    # Operator-only diagnostic record of every actual model send (docs/design/llm-request-record-tech-design.md).
    from bowhead_gateway.llm_request_recorder import start_llm_request_recorder

    _llm_request_recorder = start_llm_request_recorder(user_store.db_path)
    app.state.llm_request_recorder = _llm_request_recorder

    # KB 90-day grace hard-delete sweeper (P1-N3 / matrix FR-7 wiring: compliance debt registered at tech-design-m2.md:11).
    # Default dry-run; actual irreversible deletion requires BOWHEAD_KB_GRACE_SWEEPER=1.
    _kb_grace_sweeper_task: asyncio.Task | None = None
    _kb_files_store_ref = getattr(app.state, "kb_files_store", None)
    _kb_store_ref = getattr(app.state, "kb_store", None)
    if _kb_files_store_ref is not None and _kb_store_ref is not None:
        from bowhead_gateway.kb_grace_sweeper import (
            env_grace_days,
            run_kb_grace_sweeper,
        )
        from bowhead_gateway.kb_grace_sweeper import (
            env_interval_s as kb_grace_interval_s,
        )

        _kb_grace_sweeper_task = asyncio.create_task(
            run_kb_grace_sweeper(
                files_store=_kb_files_store_ref,
                kb_store=_kb_store_ref,
                rows_store=getattr(app.state, "kb_extracted_rows_store", None),
                matrix_store=getattr(app.state, "kb_matrix_store", None),
                interval_s=kb_grace_interval_s(),
                grace_days=env_grace_days(),
            ),
            name="kb_grace_sweeper",
        )
        log.info("kb_grace_sweeper.enabled")

    export_retention_task = asyncio.create_task(
        _export_retention_loop(),
        name="canvas-export-retention",
    )
    app.state.llm_verification_runner.start_maintenance()
    log.info("team_llm.verification_maintenance_enabled")

    uploaded_skill_metadata_task = asyncio.create_task(uploaded_skill_reference_metadata.run(), name="uploaded-skill-reference-metadata")
    from bowhead_core.system_agent_reference_metadata import SystemAgentReferenceMetadataCache

    system_agent_reference_metadata = SystemAgentReferenceMetadataCache(db_path, getattr(orchestrator, "subagent_runner", None))
    app.state.system_agent_reference_metadata = system_agent_reference_metadata
    system_agent_metadata_task = asyncio.create_task(system_agent_reference_metadata.run(), name="system-agent-reference-metadata")
    from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadataCache

    reference_receiver_metadata = ReferenceReceiverMetadataCache(db_path)
    app.state.reference_receiver_metadata = reference_receiver_metadata
    reference_receiver_metadata_task = asyncio.create_task(reference_receiver_metadata.run(), name="reference-receiver-metadata")
    from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataCache

    reference_mcp_metadata = ReferenceMcpMetadataCache(db_path)
    app.state.reference_mcp_metadata = reference_mcp_metadata
    orchestrator.reference_mcp_metadata = reference_mcp_metadata
    reference_mcp_metadata_task = asyncio.create_task(reference_mcp_metadata.run(), name="reference-mcp-metadata")
    from bowhead_gateway.document_service import DocumentService

    app.state.document_service = DocumentService(app.state)
    orchestrator.document_service = app.state.document_service
    document_task = asyncio.create_task(app.state.document_service.run(), name="template-document-jobs")
    try:
        yield
    finally:
        document_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await document_task
        reference_mcp_metadata_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await reference_mcp_metadata_task
        reference_receiver_metadata_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await reference_receiver_metadata_task
        system_agent_metadata_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await system_agent_metadata_task
        uploaded_skill_metadata_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await uploaded_skill_metadata_task
        if _terminal_writer_service is not None:
            try:
                await _terminal_writer_service.begin_drain()
            except Exception as exc:
                log.warning(
                    "terminal_writer.begin_drain_failed",
                    error=type(exc).__name__,
                )
        _terminal_recovery_triggers_enabled = False
        if _terminal_recovery_monitor_task is not None:
            _terminal_recovery_monitor_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _terminal_recovery_monitor_task
        if _terminal_auth_revoke_task is not None:
            _terminal_auth_revoke_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _terminal_auth_revoke_task
        for _terminal_task in tuple(_terminal_recovery_tasks):
            _terminal_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _terminal_task
        if _terminal_writer_service is not None:
            workers_drained = await _terminal_writer_service.wait_for_workers(
                timeout_seconds=30,
            )
            log.info(
                "terminal_writer.worker_drain_complete",
                drained=workers_drained,
            )
        log.info("gateway.stopping")
        _chat_submission_maintenance_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await _chat_submission_maintenance_task
        if openapi_log_prune_task is not None:
            openapi_log_prune_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await openapi_log_prune_task
        if scheduler is not None:
            await scheduler.stop()
        if _clarification_invalidation_publisher_task is not None:
            _clarification_invalidation_publisher_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _clarification_invalidation_publisher_task
        await ordered_turn_log_sink.drain()
        await app.state.llm_verification_runner.close()
        subagent_compatibility_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await subagent_compatibility_task
        with suppress(Exception):
            await subagent_compatibility_heartbeat.unregister()
        if subagent_draft_maintenance_task is not None:
            subagent_draft_maintenance_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await subagent_draft_maintenance_task
        if config_draft_maintenance_task is not None:
            config_draft_maintenance_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await config_draft_maintenance_task
        await export_runner.stop()
        if export_retention_task is not None:
            export_retention_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await export_retention_task
        # cca LOAD POINT: stop the tool-auth cache reaper task.
        with suppress(Exception):
            await tool_auth_registry.stop()
        with suppress(Exception):
            await _team_kb_runtime_registry.stop()
        # kb-matrix M2: stop zombie loop + running tasks (cell state is checkpoint;
        # next boot resume converges, without rerunning done cells).
        _kb_matrix_zombie_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await _kb_matrix_zombie_task
        for _mx_task in list(_kb_matrix_engine.run_tasks.values()):
            if not _mx_task.done():
                _mx_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await _mx_task
        for _status in list(getattr(app.state, "team_kb_reindex_tasks", {}).values()):
            _task = _status.get("task") if isinstance(_status, dict) else None
            if _task is not None and not _task.done():
                _task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await _task
        # Sprint file-upload-presign Phase 1.5: cancel sweeper before shutdown
        if _presign_sweeper_task is not None:
            _presign_sweeper_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _presign_sweeper_task
        if _notification_sweeper_task is not None:
            _notification_sweeper_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _notification_sweeper_task
        if _access_request_sweeper_task is not None:
            _access_request_sweeper_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _access_request_sweeper_task
        if _feedback_casework_task is not None:
            _feedback_casework_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _feedback_casework_task
        if _agent_release_outbox_task is not None:
            _agent_release_outbox_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _agent_release_outbox_task
        _agent_eval_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await _agent_eval_task
        await _agent_eval_runtime.close()
        _kb_maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await _kb_maintenance_task
        for task in list(getattr(app.state, "kb_rebuild_tasks", {}).values()):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if _kb_grace_sweeper_task is not None:
            _kb_grace_sweeper_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _kb_grace_sweeper_task
        # credit-billing gap 7: unregister sink + stop flush loop (cancellation flushes buffered writes first; normal shutdown loses none).
        if _llm_usage_recorder is not None:
            from bowhead_gateway.llm_usage_recorder import stop_llm_usage_recorder

            await stop_llm_usage_recorder(_llm_usage_recorder)
        from bowhead_gateway.llm_request_recorder import stop_llm_request_recorder

        await stop_llm_request_recorder(_llm_request_recorder)
        # P0 #5: cancel clarification-expiration task.
        if _clarify_expire_task is not None:
            _clarify_expire_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _clarify_expire_task
        if _terminal_writer_service is not None:
            with suppress(Exception):
                await _terminal_writer_service.close(worker_timeout_seconds=0)
        # #54 long-term fix: cancel artifact GC task.
        if _artifact_gc_task is not None:
            _artifact_gc_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _artifact_gc_task
        # V3 PRD-B: cancel memory GC task.
        if _memory_gc_task is not None:
            _memory_gc_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _memory_gc_task
        if _feishu_hygiene_task is not None:
            _feishu_hygiene_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _feishu_hygiene_task
        # Sprint α G3 (CTO P0-O-1 fix): cancel SMTP outbox worker
        _smtp_task = getattr(app.state, "_smtp_outbox_task", None)
        if _smtp_task is not None:
            _smtp_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _smtp_task
        # M158-18 Q12 fix: gracefully stop project_memory cron task.
        cron_task = getattr(app.state, "_project_memory_cron_task", None)
        if cron_task is not None:
            from bowhead_core.project_memory_aggregator import stop_project_memory_cron

            await stop_project_memory_cron(cron_task)
        # kb-structured-tables Sprint 2: stop catalog seed background task.
        # Idempotent — task may have already completed (paced ~100ms × N ws).
        _catalog_seed_task = getattr(app.state, "_kb_catalog_seed_task", None)
        if _catalog_seed_task is not None and not _catalog_seed_task.done():
            _catalog_seed_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _catalog_seed_task
        # kb-structured-tables Sprint 2 Phase B: cancel any in-flight backfill tasks.
        _backfill_tasks = getattr(app.state, "kb_backfill_tasks_by_id", {})
        for _bt_id, _bt in list(_backfill_tasks.items()):
            if not _bt.done():
                _bt.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await _bt
        # P4.5.13: stop user cron (GDPR purge + monthly credit reset)
        account_deletion_task = getattr(app.state, "account_deletion_task", None)
        if account_deletion_task is not None:
            account_deletion_task.cancel()
            with suppress(asyncio.CancelledError):
                await account_deletion_task
        user_cron = getattr(app.state, "user_cron_task", None)
        if user_cron is not None:
            user_cron.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await user_cron
        credit_attention_task = getattr(app.state, "credit_attention_materializer_task", None)
        if credit_attention_task is not None:
            credit_attention_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await credit_attention_task
        # Licensing: stop the local license-recheck loop (only scheduled in local mode)
        _ent_recheck = getattr(app.state, "entitlement_recheck_task", None)
        if _ent_recheck is not None:
            _ent_recheck.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _ent_recheck
        # ws-datarc-decouple [review P2-b]: close the per-team DatarcClient pool too — else the
        # pooled httpx2.AsyncClient instances (each holding an auth header) leak on shutdown/reload.
        _dc_factory = getattr(app.state, "datarc_client_factory", None)
        if _dc_factory is not None:
            await _dc_factory.aclose()
        # embed-via-openapi: close the pooled per-team embed-proxy clients (mirrors _dc_factory)
        _embed_factory = getattr(app.state, "embed_client_factory", None)
        if _embed_factory is not None:
            await _embed_factory.aclose()
        app.state.feishu_accepting_dispatch = False
        _feishu_client = getattr(app.state, "feishu_client", None)
        if _feishu_client is not None and hasattr(_feishu_client, "aclose"):
            await _feishu_client.aclose()
        if vector_client is not None:
            await vector_client.aclose()
        await provider_registry.close_all()
        from bowhead_core._db import close_pooled_readers

        await close_pooled_readers()


def _read_version() -> str:
    env_v = os.environ.get("BOWHEAD_VERSION", "").strip()
    if env_v:
        return env_v
    for p in ("/app/VERSION", str(Path(__file__).resolve().parents[4] / "VERSION")):
        try:
            with Path(p).open(encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return "dev"


async def _require_agent_marketplace_ready(request: Request) -> None:
    from bowhead_core.subagent_management_flags import (
        subagent_management_v2_ready,
    )

    ready = await subagent_management_v2_ready(
        getattr(request.app.state, "agent_assignment_store", None),
        schema_ready=bool(
            getattr(request.app.state, "agent_marketplace_schema_ready", False),
        ),
    )
    if not ready:
        raise HTTPException(
            status_code=503,
            detail={"code": "agent_marketplace_not_ready"},
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{product_name()} Gateway",
        description="北极九章 Data Agent — HTTP Gateway",
        version=_read_version(),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Starlette inserts each new user middleware at the front. Register from
    # inner to outer so the effective stack is:
    # RequestId → ConditionalHeaderCanonicalization → CORS → PublicEntry →
    # PublicSlashRedirect → CommunitySetup → MemoryMutation → ExceptionMiddleware.
    from bowhead_gateway.memory_mutation_context import (
        MemoryMutationContextMiddleware,
    )

    app.add_middleware(MemoryMutationContextMiddleware)
    from bowhead_gateway.installation import CommunitySetupMiddleware

    app.add_middleware(CommunitySetupMiddleware)
    from bowhead_gateway.public_redirects import PublicSlashRedirectMiddleware

    app.add_middleware(PublicSlashRedirectMiddleware)
    from bowhead_gateway.public_entry_context import PublicEntryContextMiddleware

    app.add_middleware(PublicEntryContextMiddleware)
    from bowhead_gateway.http_request_context import (
        RequestIdContextMiddleware,
        cors_middleware_options,
    )

    app.add_middleware(
        CORSMiddleware,
        **cors_middleware_options(),
    )
    from bowhead_gateway.conditional_headers import (
        ConditionalHeaderCanonicalizationMiddleware,
    )

    # Outside CORS so every route — hand-read or FastAPI-injected — sees canonical
    # If-Match / If-None-Match. A compressing CDN weakens the entity-tags we issue;
    # see conditional_headers.py for why we accept the weakened form.
    app.add_middleware(ConditionalHeaderCanonicalizationMiddleware)
    app.add_middleware(RequestIdContextMiddleware)

    # i18n v1: exception handlers (CodedError → 4xx rendered in user ui_language).
    from bowhead_gateway.error_handler import register_error_handlers

    register_error_handlers(app)
    from bowhead_gateway.terminal_protocol_http import (
        register_terminal_protocol_http_handler,
    )

    register_terminal_protocol_http_handler(app)

    # Sprint file-upload-presign Phase 1.2 — dev-only local receiver
    # (prod uses S3Backend, no receiver path registered). Prod startup with
    # the flag accidentally True refuses to boot. Route registered here
    # because create_app() has settings available + this is the documented
    # opt-in point per tech-design §6.3.
    _presign_settings = get_settings()
    if _presign_settings.allow_local_receiver:
        if _presign_settings.env == "prod":
            raise RuntimeError(
                "BOWHEAD_ALLOW_LOCAL_RECEIVER=1 in prod is forbidden (use S3 backend).",
            )
        from bowhead_gateway.routes import files_local_receiver as _lr

        app.include_router(_lr.router, tags=["files-local-receiver"])

    app.include_router(health.router, tags=["health"])
    # Planning Mode (M1 block 4) — dev dogfood REST surface (behind BOWHEAD_PLANNING_MODE)
    from bowhead_gateway.routes import plans as plans_routes

    app.include_router(plans_routes.router, tags=["planning"])
    # Sprint ω Phase 5: admin metrics rollup (r3.1 §2.9 replacement for NoOp prometheus)
    from bowhead_gateway.routes import admin_metrics as admin_metrics_routes

    app.include_router(admin_metrics_routes.router, prefix="/api/v1", tags=["admin-metrics"])
    app.include_router(chat.router, prefix="/api/v1/chat", tags=["chat"])
    # Multi-agent v1.5 PR-#20: H2 approval WebSocket endpoint
    from bowhead_gateway.routes import task_plan as task_plan_routes

    app.include_router(task_plan_routes.router, tags=["task-plan"])
    app.include_router(memory.router, prefix="/api/v1/memory", tags=["memory"])
    # V3.5 PRD-C: notes endpoints (mounted alongside memory routes)
    from bowhead_gateway.routes import memory_notes as memory_notes_routes

    app.include_router(
        memory_notes_routes.router,
        prefix="/api/v1/memory",
        tags=["memory"],
    )
    # V4 PRD-D-4-4: memory pack replay (user + admin scopes; routes carry their
    # own absolute paths /api/v1/memory/turns/... and /api/v1/admin/turns/...)
    from bowhead_gateway.routes import memory_replay as memory_replay_routes

    app.include_router(memory_replay_routes.router, tags=["memory_replay"])
    app.include_router(
        clarifications_routes.router,
        prefix="/api/v1/clarifications",
        tags=["clarifications"],
    )
    app.include_router(sessions.router, prefix="/api/v1/sessions", tags=["sessions"])
    from bowhead_gateway.routes import documents

    app.include_router(documents.router, prefix="/api/v1", tags=["documents"])
    app.include_router(canvas_routes.router, prefix="/api/v1/canvas", tags=["canvas"])
    from bowhead_gateway.routes import results as results_routes

    app.include_router(results_routes.router, prefix="/api/v1/results", tags=["results"])
    app.include_router(results_routes.owner_router, prefix="/api/v1", tags=["results"])
    # cca ⑤ — team tool source HITL CRUD (team-admin, team-scoped)
    from bowhead_gateway.routes import team_tool_sources as team_tool_sources_routes

    app.include_router(team_tool_sources_routes.router, prefix="/api/v1/team-tool-sources", tags=["cca"])
    from bowhead_gateway.routes import team_builtin_tools as team_builtin_tools_routes

    app.include_router(team_builtin_tools_routes.router, prefix="/api/v1/team-builtin-tools", tags=["team-tools"])
    from bowhead_gateway.routes import personal_mcp as personal_mcp_routes

    app.include_router(personal_mcp_routes.router, prefix="/api/v1/personal-mcp", tags=["personal-mcp"])
    from bowhead_gateway.routes import google_workspace as google_workspace_routes

    # Always mount; FEATURE_GOOGLE_WORKSPACE OFF returns 404 google_workspace_unavailable per request.
    app.include_router(google_workspace_routes.router, prefix="/api/v1/google", tags=["google-workspace"])
    # agent-feedback-loop batch 2b-i: suggestion-inbox approval UI. Always mount (same as marketplace's
    # advertised-but-unmounted drift prevention); router dependencies evaluate per request.
    from bowhead_gateway.routes import feedback_candidates as feedback_candidates_routes

    app.include_router(
        feedback_candidates_routes.router,
        prefix="/api/v1/feedback-candidates",
        tags=["feedback"],
    )
    # agent-feedback-loop batch 3d: direct coaching (handle users' own imported skills without candidates). Always mount as above; OFF→404.
    from bowhead_gateway.routes import feedback_coach as feedback_coach_routes

    app.include_router(
        feedback_coach_routes.router,
        prefix="/api/v1/feedback-coach",
        tags=["feedback"],
    )
    # ws-datarc-decouple P2b (③): team datarc connection CRUD (admin-only)
    from bowhead_gateway.routes import (
        datarc_connections as datarc_connections_routes,
    )

    app.include_router(
        datarc_connections_routes.router,
        prefix="/api/v1/datarc-connections",
        tags=["datarc-connections"],
    )
    # Power BI Service Principal configuration, discovery and DAX template APIs.
    # Team subagent authoring CRUD is member-open and team-scoped.
    from bowhead_core.subagent_creator_trigger import subagent_creator_enabled

    if subagent_creator_enabled():
        from bowhead_gateway.routes import (
            team_subagent_drafts as team_subagent_drafts_routes,
        )
        from bowhead_gateway.routes import team_subagents as team_subagents_routes

        # Register the static /drafts route before /{subagent_id}.
        app.include_router(
            team_subagent_drafts_routes.router,
            prefix="/api/v1/team-subagents/drafts",
            tags=["cca"],
        )
        app.include_router(team_subagents_routes.router, prefix="/api/v1/team-subagents", tags=["cca"])
    # Mount the Marketplace facade unconditionally. Runtime admission below is
    # shared with /auth/me, so changing deployment configuration cannot leave a
    # process with advertised-but-unmounted (or mounted-but-unready) routes.
    from bowhead_gateway.routes import agents as agents_routes

    app.include_router(
        agents_routes.router,
        prefix="/api/v1/agents",
        tags=["agents"],
        dependencies=[Depends(_require_agent_marketplace_ready)],
    )
    from bowhead_gateway.routes import agent_coach as agent_coach_routes

    app.include_router(agent_coach_routes.router, prefix="/api/v1/agent-coach", tags=["agent-coach"])

    from bowhead_gateway.routes import capability_catalog as capability_catalog_routes
    from bowhead_gateway.routes import team_plugins as team_plugins_routes

    app.include_router(capability_catalog_routes.router, prefix="/api/v1/capability-catalog", tags=["capability-catalog"])
    from bowhead_gateway.routes import builtin_resources as builtin_resources_routes

    app.include_router(builtin_resources_routes.router, prefix="/api/v1", tags=["builtin-resources"])

    app.include_router(
        team_plugins_routes.router,
        prefix="/api/v1/team-capabilities",
        tags=["team-capabilities"],
    )
    # workflow-solidify S1: distill endpoint (dark until enabled, mirrors subagent_creator's master switch).
    # Canonical flag helper: bowhead_core.workflow_creator_trigger (same source as trigger registration, one flag without drift).
    from bowhead_core.workflow_creator_trigger import workflow_creator_enabled

    if workflow_creator_enabled():
        from bowhead_gateway.routes import workflow_solidify as workflow_solidify_routes

        app.include_router(
            workflow_solidify_routes.router,
            prefix="/api/v1/workflow-solidify",
            tags=["cca"],
        )
        # S4: team-shared recipe CRUD (same single workflow_creator flag).
        from bowhead_gateway.routes import team_skills as team_skills_routes

        app.include_router(team_skills_routes.router, prefix="/api/v1/team-skills", tags=["cca"])
    # config-copilot batch 0: the form registry (idempotent replace) and its routes are always mounted;
    # the FEATURE_CONFIG_COPILOT gate was removed on 2026-08-18.
    from bowhead_gateway.config_copilot_forms import register_gateway_forms

    register_gateway_forms(app)
    from bowhead_gateway.routes import config_drafts as config_drafts_routes
    from bowhead_gateway.routes import config_proposals as config_proposals_routes

    app.include_router(config_drafts_routes.router, tags=["config-copilot"])
    # Batch 1-a: lightweight-confirmation proposal endpoints (terminal source of truth + server apply/undo/ignore).
    app.include_router(config_proposals_routes.router)
    # Team mail service: mount when enabled (local/SaaS); SMTP destinations use shared outbound policy by deployment mode.
    from bowhead_gateway.routes import admin_email_config as admin_email_config_routes

    if admin_email_config_routes.email_config_router_enabled():
        app.include_router(
            admin_email_config_routes.router,
            prefix="/api/v1/admin/email-config",
            tags=["admin"],
        )
    # Initiators confirm each send_email before sending (tool and confirmation endpoints always registered).
    from bowhead_gateway.routes import email_send_confirmations as email_send_confirmation_routes

    app.include_router(
        email_send_confirmation_routes.router,
        prefix="/api/v1/email-send-confirmations",
        tags=["chat"],
    )
    from bowhead_gateway.routes import shopify_content_confirmations as shopify_content_confirmation_routes

    app.include_router(shopify_content_confirmation_routes.router, prefix="/api/v1/shopify-content-confirmations", tags=["chat"])
    # P4.5.1 auth
    from bowhead_gateway.routes import auth as auth_routes

    app.include_router(auth_routes.router, prefix="/api/v1/auth", tags=["auth"])
    from bowhead_gateway.routes import invite_activation as invite_activation_routes

    app.include_router(
        invite_activation_routes.router,
        prefix="/api/v1/auth",
        tags=["auth"],
    )
    from bowhead_gateway.routes import credential_recovery as credential_recovery_routes

    app.include_router(
        credential_recovery_routes.public_router,
        prefix="/api/v1/auth",
        tags=["auth"],
    )
    app.include_router(
        credential_recovery_routes.admin_router,
        tags=["admin"],
    )
    from bowhead_gateway.routes import bootstrap as bootstrap_routes

    app.include_router(bootstrap_routes.router, tags=["bootstrap"])
    from bowhead_gateway.routes import team_onboarding as team_onboarding_routes

    app.include_router(team_onboarding_routes.router, tags=["team-onboarding"])
    from bowhead_gateway.routes import access_control as access_control_routes

    app.include_router(access_control_routes.router, prefix="/api/v1", tags=["access-control"])
    # content-permissions wave 1 — user-initiated Share API. The flag (default ON)
    # gates the whole route group; per-kind the capability assets additionally
    # follow their creation flags inside the adapter (tech-design §3/§5.1).
    from bowhead_core.share_service import content_sharing_enabled

    if content_sharing_enabled():
        from bowhead_gateway.routes import shares as shares_routes

        app.include_router(shares_routes.router, prefix="/api/v1/shares", tags=["sharing"])
        # S6 principal picker (minimal member-readable directory), same lifecycle as shares.
        app.include_router(shares_routes.directory_router, prefix="/api/v1/team", tags=["sharing"])
    # credential-readiness D5: capability contacts (minimal set of who can enable); no flag, always mounted.
    from bowhead_gateway.routes import team_capability_contacts as _capability_contacts_routes

    app.include_router(_capability_contacts_routes.router, prefix="/api/v1/team", tags=["team"])
    # credential-readiness R9: three KB capability states (member-readable, used for /knowledge inline notices).
    from bowhead_gateway.routes import team_kb_capabilities as _kb_capabilities_routes

    app.include_router(_kb_capabilities_routes.router, prefix="/api/v1/team", tags=["team"])
    # content-permissions wave 2: access-request + approval flow (own flag, default ON).
    from bowhead_core.access_request_flags import access_requests_enabled

    if access_requests_enabled():
        from bowhead_gateway.routes import access_requests as access_requests_routes

        app.include_router(
            access_requests_routes.router,
            prefix="/api/v1/access-requests",
            tags=["access-requests"],
        )
    # V7 KB sprint Day 16: register KB router (includes its own /api/v1/kb prefix).
    from bowhead_gateway.routes import kb as kb_routes

    app.include_router(kb_routes.router, tags=["knowledge-base"])
    # Option A sidebar V3: permission-preserving cross-object search.
    from bowhead_gateway.routes import unified_search as unified_search_routes

    app.include_router(unified_search_routes.router)
    # kb-doctype-config V2: doc_types CRUD + LLM column gen
    from bowhead_gateway.routes import kb_doc_types as kb_doc_types_routes

    app.include_router(kb_doc_types_routes.router, tags=["knowledge-base"])
    # kb-knowledge-tables sprint v1.0: extracted_records CRUD + field edit + verify_status
    from bowhead_gateway.routes import kb_records as kb_records_routes

    app.include_router(kb_records_routes.router, tags=["knowledge-base"])
    # kb-structured-tables Sprint 2 Phase B: admin backfill (re-run classifier+extract
    # on existing parsed files; workspace_admin-only, per-ws lock, cursor pagination)
    from bowhead_gateway.routes import kb_admin as kb_admin_routes

    app.include_router(kb_admin_routes.router, tags=["knowledge-base-admin"])
    # kb-matrix M2: document-matrix endpoints.
    from bowhead_gateway.routes import kb_matrix as kb_matrix_routes

    app.include_router(kb_matrix_routes.router, tags=["knowledge-base"])
    # kb-matrix-sharing: team-template-library endpoints.
    from bowhead_gateway.routes import kb_team_templates as kb_team_templates_routes

    app.include_router(kb_team_templates_routes.router, tags=["knowledge-base"])
    # licensing §9 / ARCH-2: the P4.5.2 teams router (4 unauthenticated team-mutation routes + 2 GETs)
    # is RETIRED — its mutations are superseded by the caller-team-bound admin_billing twins
    # (/api/v1/admin/team/* §7.1) and its team-meta read by GET /api/v1/admin/credits. FE repointed.
    # P4.5.6 workspace member management
    from bowhead_gateway.routes import members as members_routes

    app.include_router(members_routes.router, prefix="/api/v1/workspaces", tags=["members"])
    # P4.5.5 profile + API tokens + GDPR
    from bowhead_gateway.routes import profile as profile_routes

    app.include_router(profile_routes.router, prefix="/api/v1/profile", tags=["profile"])
    # P4.5.4 Feishu OAuth
    from bowhead_gateway.routes import integration_overview as integration_overview_routes
    from bowhead_gateway.routes import oauth as oauth_routes
    from bowhead_gateway.routes import teams as teams_routes

    app.include_router(oauth_routes.router, prefix="/api/v1/auth/oauth", tags=["oauth"])
    app.include_router(approvals_routes.router, prefix="/api/v1/approvals", tags=["approvals"])
    app.include_router(notifications_routes.router, prefix="/api/v1/notifications", tags=["notifications"])
    app.include_router(home_routes.router, prefix="/api/v1/home", tags=["home"])
    app.include_router(home_proposal_routes.router, prefix="/api/v1/home", tags=["home"])
    app.include_router(
        feishu_routes.router,
        prefix="/api/v1/integrations/feishu",
        tags=["feishu"],
    )
    app.include_router(
        slack_routes.router,
        prefix="/api/v1/integrations/slack",
        tags=["slack"],
    )
    app.include_router(
        teams_routes.router,
        prefix="/api/v1/integrations/teams",
        tags=["teams"],
    )
    app.include_router(integration_overview_routes.router)
    # workflow-solidify §9-B (companion P0): all 9 turn routes previously lacked authorization. Check anonymous denial +
    # turn→session→user ownership centrally in each {turn_id} handler's _verify_turn_access
    # (turns.py:_verify_turn_access calls _current_user), without router-level dependencies
    # (bound at create_app, complicating test override timing). list_turns fetches candidates from canonical turns
    # within current team/user scope, then rechecks each turn's access.
    app.include_router(turns_routes.router, prefix="/api/v1/turns", tags=["turns"])
    app.include_router(memory_candidates_routes.router, prefix="/api/v1/memory/candidates", tags=["memory"])
    app.include_router(datarc_routes.router, prefix="/api/v1/datarc", tags=["datarc"])
    app.include_router(files.router, prefix="/api/v1/files", tags=["files"])
    # Sprint file-upload-presign Phase 1.4a: chat-attachment presign + confirm
    # (KB single + bundle presign live in routes/kb.py, Phase 1.4b).
    from bowhead_gateway.routes import files_presign as files_presign_routes

    app.include_router(files_presign_routes.router, tags=["files-presign"])
    # Sprint file-upload-presign Phase 1.4b: KB single-doc presign + confirm.
    # KB bundle presign lands in Phase 1.4c.
    from bowhead_gateway.routes import kb_presign as kb_presign_routes

    app.include_router(kb_presign_routes.router, tags=["kb-presign"])

    # V3 admin credits: team-admin quota management + overview + audit.
    from bowhead_gateway.routes import admin_credits as admin_credits_routes

    app.include_router(
        admin_credits_routes.router,
        prefix="/api/v1/admin/credits",
        tags=["admin-credits"],
    )

    from bowhead_gateway.routes import team_operations as team_operations_routes

    app.include_router(team_operations_routes.router, prefix="/api/v1/team-operations", tags=["team-operations"])

    # /api/v1/admin/agents removed 2026-08-04: multi-agent-v2 PR-#5 workspace-level
    # YAML manifest CRUD. app.state.manifest_admin_store was never set in lifespan, always returning production 503;
    # FE page lacked navigation too, leaving dead code guarded only by its own unit tests. Team governance of builtin subagents uses
    # /api/v1/team-subagents/builtins/{name}/override; see docs/sprints/builtin-subagent-admin/.

    # V2.1.5: user settings (PPTX template persistence, etc.).
    from bowhead_gateway.routes import user_settings as user_settings_routes

    app.include_router(user_settings_routes.router)

    # Theme config M1: team theme policy + effective resolver
    from bowhead_gateway.routes import team_settings as team_settings_routes

    app.include_router(team_settings_routes.router)
    from bowhead_gateway.routes import team_chart_styles as team_chart_styles_routes

    app.include_router(team_chart_styles_routes.router)
    from bowhead_core.team_openapi_flags import team_openapi_enabled

    if team_openapi_enabled():
        from bowhead_gateway.routes import team_openapi as team_openapi_routes

        app.include_router(team_openapi_routes.manage_router)
        app.include_router(team_openapi_routes.api_router)
    from bowhead_gateway.routes import public_entries as public_entries_routes

    app.include_router(public_entries_routes.router)
    from bowhead_gateway.routes import theme as theme_routes

    app.include_router(theme_routes.router)

    # i18n v1: workspace settings (default_data_language / week_start_day / i18n_v1)
    from bowhead_gateway.routes import workspace_settings as workspace_settings_routes

    app.include_router(workspace_settings_routes.router)

    from bowhead_gateway.routes import space_instructions as space_instructions_routes

    app.include_router(space_instructions_routes.router)

    # Members share a conversation Turn with the answering Agent's Owner as an evaluation-case source.
    from bowhead_gateway.routes import turn_eval_shares as turn_eval_shares_routes

    app.include_router(turn_eval_shares_routes.router)

    # V2.8 A2: frontend telemetry endpoint (allowlisted to bound Prometheus cardinality).
    from bowhead_gateway.routes import metrics_ingest as metrics_ingest_routes

    app.include_router(metrics_ingest_routes.router, prefix="/api/v1/metrics", tags=["metrics"])
    # workspaces_routes (space suggestions) removed 2026-08-07: its sole endpoint
    # GET /{workspace_id}/suggestions always returned 503 after 9e186d5d2 refactor(datarc): 团队连接收口
    # (2026-07-23), which removed app.state.datarc assignment; route dependency
    # Depends(_datarc) still read that unset field, so FastAPI rejected before entering the handler.
    # FE never called it; zero suggestions_v1 cache rows on 116. See removal commit for details.
    app.include_router(skills_routes.router, prefix="/api/v1/skills", tags=["skills"])
    # /api/v1/admin/routing (in-process primary override) removed in WS0 —
    # team-level model selection is now carried by the team LLM config agent bindings
    # (Models settings page), not a process-memory override table.
    # #84 scheduled jobs（Step 1：CRUD only；Step 2 = scheduler worker daemon）
    from bowhead_gateway.routes import scheduled as scheduled_routes

    app.include_router(scheduled_routes.router, prefix="/api/v1/scheduled", tags=["scheduled"])
    # #90 legacy custom subagents: G2 consolidation, flag OFF (default) skips mounting (product path=team-subagents).
    from bowhead_core.custom_subagent_store import user_custom_subagents_enabled

    if user_custom_subagents_enabled():
        from bowhead_gateway.routes import custom_subagents as custom_subagents_routes

        app.include_router(custom_subagents_routes.router, prefix="/api/v1/subagents", tags=["subagents"])

    # Load the named Business adapter from the fixed build manifest.
    from bowhead_gateway.edition_extensions import install_gateway_extensions

    install_gateway_extensions(app)

    # Sprint β G5 — admin members (multi-admin promote/demote)
    from bowhead_gateway.routes import admin_members as admin_members_routes

    app.include_router(admin_members_routes.router, tags=["admin-members"])

    # Space administration M0/M1 — API stays mounted while the V2 UI capability
    # is default-off; authorization and lifecycle contracts are never flag-gated.
    from bowhead_gateway.routes import admin_spaces as admin_spaces_routes

    app.include_router(admin_spaces_routes.router, tags=["admin-spaces"])

    # Sprint β G7 — admin billing (BYO LLM key + credit quota)
    from bowhead_gateway.routes import admin_billing as admin_billing_routes

    app.include_router(admin_billing_routes.router, tags=["admin-billing"])
    from bowhead_gateway.routes import (
        admin_llm_config_v2 as admin_llm_config_v2_routes,
    )

    app.include_router(admin_llm_config_v2_routes.router, tags=["admin-llm-config-v2"])

    from bowhead_gateway.routes import bowra_connect

    app.include_router(bowra_connect.router)

    # ── Frontend static hosting (production single image: i18n dual builds + content negotiation) ──
    # BOWHEAD_FRONTEND_STATIC_DIR points to the frontend-static parent, containing:
    #   zh-CN/ ← static output built with NEXT_PUBLIC_BUILD_LOCALE=zh-CN
    #   en-US/ ← static output built with NEXT_PUBLIC_BUILD_LOCALE=en-US
    # Serve the explicit NEXT_LOCALE build, otherwise English, without a hydration language change.
    # Legacy layout: no {locale}/ subdirectories (old single-locale builds) → fall back to parent directory.
    _static_dir = os.environ.get("BOWHEAD_FRONTEND_STATIC_DIR")
    if _static_dir and Path(_static_dir).is_dir():
        from bowhead_gateway.public_branding import resolve_public_branding
        from bowhead_gateway.static_localized import (
            RuntimeBasePathStaticFiles,
            frontend_static_app,
        )

        async def _public_brand_provider(context):  # type: ignore[no-untyped-def]
            return await resolve_public_branding(app, context)

        zh_dir = Path(_static_dir) / "zh-CN"
        en_dir = Path(_static_dir) / "en-US"
        de_dir = Path(_static_dir) / "de-DE"
        fr_dir = Path(_static_dir) / "fr-FR"
        if zh_dir.is_dir() and en_dir.is_dir():
            from bowhead_gateway.static_localized import LocalizedStaticFiles

            app.mount(
                "/",
                frontend_static_app(
                    LocalizedStaticFiles(
                        zh_dir=zh_dir,
                        en_dir=en_dir,
                        de_dir=de_dir if de_dir.is_dir() else None,
                        fr_dir=fr_dir if fr_dir.is_dir() else None,
                        public_brand_provider=_public_brand_provider,
                    ),
                ),
                name="frontend",
            )
            logging.getLogger("bowhead_gateway.main").info(
                "frontend.static_mounted_localized zh=%s en=%s de=%s fr=%s",
                zh_dir,
                en_dir,
                de_dir if de_dir.is_dir() else "fallback-to-en",
                fr_dir if fr_dir.is_dir() else "fallback-to-en",
            )
        else:
            # Compatibility with old single-locale builds (CI / old images).
            app.mount(
                "/",
                frontend_static_app(
                    RuntimeBasePathStaticFiles(
                        directory=_static_dir,
                        html=True,
                        public_brand_provider=_public_brand_provider,
                    ),
                ),
                name="frontend",
            )
            logging.getLogger("bowhead_gateway.main").info(
                "frontend.static_mounted_single dir=%s",
                _static_dir,
            )

    return app


app = create_app()
