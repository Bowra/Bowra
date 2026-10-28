"""共享 FastAPI Depends 工厂.

require_team_admin — 当前团队 owner/team_admin 门；credit 系依赖按
credit_capabilities_for_role 的角色能力矩阵执行；审计管理员和普通成员不取得额度管理权。

设计见 docs/V3-admin-credits-tech-design.md §1.1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from bowhead_core.credit_activation import (
    CreditActorCapabilities,
    credit_capabilities_for_role,
)
from fastapi import Depends, HTTPException

from bowhead_gateway.routes.auth import _current_user, _store

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

_log = logging.getLogger(__name__)

_CURRENT_USER_DEP = Depends(_current_user)
_STORE_DEP = Depends(_store)


class RouterMiss(StrEnum):
    """团队 router 解析失败的成因（批 2-a tech design §3.1）。

    **只做可观测维度，不做 HTTP code 维度**：对外一律出已注册的
    ``team_model_unconfigured``；成因进日志与 ``bowhead_team_router_miss_total``。
    新增 user-facing code 会撞三道集合相等闸（published_diagnostics 的
    (phase, failure_class, retryable) 表 / test_skill_workflow_error_contract 的
    AST 白名单 / test_error_catalog 的 documented==zh==en），且漏注册时
    error_catalog 会把 code 字符串本身当文案返给用户。
    """

    UNCONFIGURED = "unconfigured"  # No llm_config / empty providers.
    LEGACY_CONFIG = "legacy_config"  # Unmigrated v1 config (providers is a list).
    BUILD_FAILED = "build_failed"  # Valid v2, build failed (bad credentials / unreachable provider).
    RESOLVE_ERROR = "resolve_error"  # DB / registry error.
    NO_BINDING = "no_binding"  # Router built without this agent_type binding.


def _record_router_miss(miss: RouterMiss, *, site: str) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_team_router_miss_total",
            labels={"reason": miss.value, "site": site},
            help="Team router resolution misses, by cause and call site.",
        )
    except Exception:  # Metrics must not disrupt business flow, but cannot be silent; silence is the defect this batch fixes.
        _log.warning("team_router.miss_metric_failed reason=%s site=%s", miss.value, site, exc_info=True)


async def resolve_team_router(
    app_state: object,
    team_id: str | None,
    *,
    agent_type: str | None = None,
    site: str = "unknown",
) -> tuple[object | None, RouterMiss | None]:
    """团队 router 的唯一解析入口，返回 ``(router, None)`` 或 ``(None, miss)``。

    **绝不返回 app.state.router**（批 2-a §3.1）：降级的正确形态是「不调 LLM」，
    不是「调一个注定 refuse 的 router」——后者会把 refusal 计数灌成噪声。

    成因**在本函数内自推**，不改 ``TeamRouterRegistry`` 的跨包接口：那两层
    （``resolve_team_byo_overrides`` / ``TeamRouterRegistry.get``）都把四类原因
    折叠成同一个 None，照它们的返回值判会让「配了但配坏了」的团队被告知
    「未配置」，被指去重配一个已经配好的东西。

    ``LEGACY_CONFIG`` 的语义是「v1 配置且**迁不出来**」，不是「是 v1」——
    ``migrate_legacy_config_to_v2`` 有成功路径，真实 legacy 行大多能迁出可用配置，
    而全仓其它 router 读者都在照常迁移它们。把「是 v1」判成 miss 会让配得好好的
    legacy 团队被告知「未配置」（实现层 review P0）。
    """
    if not team_id:
        _record_router_miss(RouterMiss.UNCONFIGURED, site=site)
        return None, RouterMiss.UNCONFIGURED

    user_store = getattr(app_state, "user_store", None)
    registry = getattr(app_state, "team_router_registry", None)
    if user_store is None or registry is None:
        _record_router_miss(RouterMiss.RESOLVE_ERROR, site=site)
        return None, RouterMiss.RESOLVE_ERROR

    try:
        raw, version = await user_store.get_team_llm_config_with_version(team_id)
    except Exception:
        _log.warning("team_router.config_read_failed team=%s site=%s", team_id, site, exc_info=True)
        _record_router_miss(RouterMiss.RESOLVE_ERROR, site=site)
        return None, RouterMiss.RESOLVE_ERROR

    # ① Entirely unconfigured: empty raw or missing/empty providers container.
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not raw or not providers:
        _record_router_miss(RouterMiss.UNCONFIGURED, site=site)
        return None, RouterMiss.UNCONFIGURED

    # ② **Migrate first, then classify**. Initial design checked v1 shape before migration, assuming
    # migrate_legacy_config_to_v2 only returns {} for legacy. **That premise is false**
    # (implementation review P0): it has a successful path (team_llm_config.py:212-222).
    # Real legacy rows (key-only provider rows / providers[] + agent_routing) can migrate to
    # usable v2 config; every other repository router reader (chat / canvas /
    # home_proposals / turn_service …) migrates them normally.
    # The old criterion tells a **correctly configured** legacy team that its model is unconfigured
    # across 11 feature surfaces while chat works, a worse contradiction than the defect being fixed.
    #
    # Thus LEGACY_CONFIG means v1 **that cannot migrate**, rather than merely v1.
    try:
        from bowhead_core.team_llm_config import migrate_legacy_config_to_v2

        migrated = migrate_legacy_config_to_v2(raw, loaded_config=getattr(app_state, "routing_config_loaded", None))
    except Exception:
        _log.warning("team_router.migrate_failed team=%s site=%s", team_id, site, exc_info=True)
        _record_router_miss(RouterMiss.LEGACY_CONFIG, site=site)
        return None, RouterMiss.LEGACY_CONFIG

    if not migrated:
        # raw has providers but cannot migrate to v2: configured yet unusable v1.
        _record_router_miss(RouterMiss.LEGACY_CONFIG, site=site)
        return None, RouterMiss.LEGACY_CONFIG

    try:
        router = await registry.get(team_id, migrated, version)
    except Exception:
        _log.warning("team_router.build_failed team=%s site=%s", team_id, site, exc_info=True)
        _record_router_miss(RouterMiss.BUILD_FAILED, site=site)
        return None, RouterMiss.BUILD_FAILED

    # ③ Valid v2 that cannot build = bad credentials / unusable provider configuration.
    if router is None:
        _record_router_miss(RouterMiss.BUILD_FAILED, site=site)
        return None, RouterMiss.BUILD_FAILED

    # ④ Binding precheck: canvas and home_proposals already do this; consolidate in the resolver
    # (batch 2-a §3.1), rather than delegating a third copy to callers.
    if agent_type is not None:
        try:
            router.resolve(agent_type)
        except KeyError:
            _log.warning("team_router.no_binding team=%s agent_type=%s site=%s", team_id, agent_type, site)
            _record_router_miss(RouterMiss.NO_BINDING, site=site)
            return None, RouterMiss.NO_BINDING

    return router, None


async def team_router_or_409(
    app_state: object,
    team_id: str | None,
    *,
    agent_type: str | None = None,
    site: str = "unknown",
    entrypoint: Literal["plan", "tool_continuation"] | None = None,
) -> object:
    """credential-readiness D2.2：解析团队 router，未配置抛与 chat 同码的 409。

    既有调用点使用 :func:`resolve_team_router` 的细分 miss 与 binding 预检；
    Composer 批 0 调用点显式传 ``entrypoint``，复用 typed
    ``resolve_team_model_runtime``。两条路径都只解析当前团队配置，绝不回退
    进程级 Router；对外错误码保持 ``team_model_unconfigured``。
    """
    if entrypoint is not None:
        from bowhead_gateway.turn_service import resolve_team_model_runtime

        resolved = await resolve_team_model_runtime(
            app_state,
            team_id,
            entrypoint=entrypoint,
        )
        if not resolved.available:
            raise HTTPException(409, {"error": {"code": "team_model_unconfigured"}})
        router = resolved.router
        if agent_type is not None:
            try:
                router.resolve(agent_type)
            except KeyError as exc:
                raise HTTPException(409, {"error": {"code": "team_model_unconfigured"}}) from exc
        return router

    router, miss = await resolve_team_router(app_state, team_id, agent_type=agent_type, site=site)
    if router is None:
        _log.warning("team_router.miss team=%s site=%s reason=%s", team_id, site, miss.value if miss else "unknown")
        raise HTTPException(409, {"error": {"code": "team_model_unconfigured"}})
    return router


async def require_team_admin(
    user: User = _CURRENT_USER_DEP,
    store: UserStore = _STORE_DEP,
) -> User:
    """Gate: owner(creator) 或当前团队 active team_admin 可过."""
    if not await store.is_team_admin(user.id, user.team_id):
        raise HTTPException(403, "team admin required")
    return user


@dataclass(frozen=True)
class CreditAdminPrincipal:
    user: User
    team_role: str
    capabilities: CreditActorCapabilities


CreditCapabilityName = Literal[
    "credits_read",
    "credit_grant",
    "credit_user_quota_write",
    "credit_user_tier_write",
    "credit_period_reset",
    "credit_team_quota_write",
    "credit_attention_dismiss",
]


async def require_credit_read(
    user: User = _CURRENT_USER_DEP,
    store: UserStore = _STORE_DEP,
) -> CreditAdminPrincipal:
    role = await store.get_team_role(user.id, user.team_id) or "member"
    capabilities = credit_capabilities_for_role(role)
    if not capabilities.credits_read:
        raise HTTPException(
            403,
            {"code": "insufficient_credit_capability", "capability": "credits_read"},
        )
    return CreditAdminPrincipal(user=user, team_role=role, capabilities=capabilities)


_CREDIT_READ_DEP = Depends(require_credit_read)


def require_credit_capability(capability: CreditCapabilityName):
    async def _dep(
        principal: CreditAdminPrincipal = _CREDIT_READ_DEP,
    ) -> CreditAdminPrincipal:
        if not getattr(principal.capabilities, capability):
            raise HTTPException(
                403,
                {"code": "insufficient_credit_capability", "capability": capability},
            )
        return principal

    return _dep
