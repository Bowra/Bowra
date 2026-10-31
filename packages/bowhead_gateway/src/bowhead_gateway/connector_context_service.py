"""批2 (workspace-space-split) — 用户级 connector 上下文服务：可信身份 + 可及集合缓存.

PRD §3.2/§3.5：
- 可信身份阶梯：L1 verified email → fetch_account；L2 IM 平台身份（feishu，c7 接入）；
  L3 admin 显式映射——真实存储形状 kind='tool_context'、key=`datarc/account`
  （slot=account；旧注释虚构的 kind='datarc_account' 从未实现）。仅未验证自报
  email = 不可信 → 空。身份语义借宿上下文存储的 slot 分槽，v2 已记入 SSOT
  三轴矩阵（docs/tool-context-framework.md）。
- 可及集合：list_account_projects 实时 + 进程内 TTL 15min 正缓存 + 5min 负缓存
  （防无账号用户每 turn 打 datarc）；手动 invalidate 通道（工具面板「重新检测」）。
- 不落表（业界同款：资源列表 tool 层实时，选择才持久）。

This service resolves verified per-user Datarc identity without creating or
changing Bowhead Space memberships.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def _metric(outcome: str) -> None:
    """Record accessible-set lookup outcomes without interrupting the caller."""
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_datarc_accessible_lookup_total",
            labels={"outcome": outcome},
            help="批2: per-user 可及集合解析结果分类 (cache_hit/resolved/no_identity/zero_workspaces/list_error)",
        )
    except Exception:  # noqa: S110
        pass


_POSITIVE_TTL_S = 15 * 60
_NEGATIVE_TTL_S = 5 * 60


@dataclass(frozen=True)
class DatarcWorkspaceRef:
    """可及集合元素——(id, name, desc) 快照，turn-start 注入 + provenance 复用（PRD §3.3b/§3.11）.

    BUG2：desc 携带工作区描述，供「让模型判断」(skip) 时 Lead 按元数据路由到相关工作区。"""

    id: str
    name: str
    desc: str = ""

    def as_dict(self) -> dict[str, str]:
        d = {"id": self.id, "name": self.name}
        if self.desc:
            d["desc"] = self.desc
        return d


@dataclass
class _CacheEntry:
    refs: tuple[DatarcWorkspaceRef, ...]
    account_uid: str
    # bi-absent-turn R3.1: empty refs reason ("no_identity" | "zero_workspaces";
    # nonempty refs = ""). Shares the refs' lifecycle (same entry, no separate caching/expiry).
    reason: str = ""
    client: Any = field(default=None, repr=False)
    at: float = field(default_factory=time.monotonic)

    def fresh(self) -> bool:
        ttl = _POSITIVE_TTL_S if self.refs else _NEGATIVE_TTL_S
        return (time.monotonic() - self.at) < ttl


class ConnectorContextService:
    """per-user datarc 可及集合解析 + 缓存。进程单例（gateway lifespan 挂 app.state）."""

    def __init__(self, *, user_store: Any, user_memory_store: Any) -> None:
        self._user_store = user_store
        self._user_memory = user_memory_store
        self._cache: dict[tuple[int, str], _CacheEntry] = {}

    # ── Trusted identity ladder (PRD §3.2) ─────────────────────

    async def resolve_trusted_account_uid(self, datarc_client: Any, user_id: str) -> str:
        """L1 verified email → L2 IM（c7 接入点，见下）→ L3 admin 映射。无可信身份返 ''.

        fail-closed：任何一级抛错记 warning 落到下一级，全空返 ''（BI 优雅缺席）。"""
        uid, _reason = await self._resolve_identity(datarc_client, user_id)
        return uid

    async def _resolve_identity(
        self,
        datarc_client: Any,
        user_id: str,
    ) -> tuple[str, str]:
        """Return (account_uid, absence_reason); failed lookups are never cached.

        Distinguish a confirmed missing account from a failed lookup. Neither
        credential rejection nor temporary failures may become a five-minute
        negative cache entry, just as project-list failures are not cached."""
        from bowhead_bi_bridge import BIPermissionError

        failure_reason = ""
        # L1: verified email
        email = ""
        try:
            u = await self._user_store.get_user(user_id)
            if u is not None and bool(getattr(u, "email_verified", False)):
                email = (getattr(u, "email", "") or "").strip()
        except Exception as e:
            failure_reason = "transient"
            logger.warning("tool_context.user_lookup_failed", user_id=user_id, err=str(e)[:120])
        if email and datarc_client is not None:
            try:
                acc = await datarc_client.fetch_account(email)
                uid = str((acc or {}).get("uid") or (acc or {}).get("accountUid") or "")
                if uid:
                    return uid, ""
                # acc is None/no uid = Datarc explicitly found no account, not a transient failure; descend to L3.
            except Exception as e:
                # Missing accounts return None; exceptions describe a failed authorization lookup.
                failure_reason = "connection_rejected" if isinstance(e, BIPermissionError) and e.source == "http" else "transient"
                logger.warning("tool_context.fetch_account_failed", user_id=user_id, err=str(e)[:120])

        # L2: IM platform identity (reverse lookup through Feishu/Lark contact API), wired in batch 2/c7.
        # Integration point: query Feishu binding row (provenance=platform) -> email -> fetch_account.

        # L3: explicit admin mapping, kind='tool_context', key=`datarc/account` (slot=account;
        # the old comment's kind='datarc_account' was never implemented; get_tool_context reads it on the next line).
        try:
            ctx = await self._user_memory.get_tool_context(user_id, "datarc", slot="account")
            uid = str((ctx or {}).get("id") or "")
            if uid:
                return uid, failure_reason
        except Exception as e:
            failure_reason = "transient"
            logger.warning("tool_context.l3_lookup_failed", user_id=user_id, err=str(e)[:120])
        return "", failure_reason

    # ── Accessible set (PRD §3.5) ─────────────────────────────

    async def accessible_workspaces(
        self,
        datarc_client: Any,
        user_id: str,
        *,
        refresh: bool = False,
    ) -> tuple[DatarcWorkspaceRef, ...]:
        """该用户可及的北极九章工作区集合（缓存后）。无身份/无账号/无工作区 → 空 tuple.

        bi-absent-turn R3.1: 薄壳——原因枚举见 ``accessible_workspaces_ex``。旧调用
        （picker `auth.py` + 存量测试）零改动。"""
        refs, _reason = await self.accessible_workspaces_ex(datarc_client, user_id, refresh=refresh)
        return refs

    async def accessible_workspaces_ex(
        self,
        datarc_client: Any,
        user_id: str,
        *,
        refresh: bool = False,
    ) -> tuple[tuple[DatarcWorkspaceRef, ...], str]:
        """Return accessible workspaces and the reason an authorization lookup is unavailable.

        Reasons are empty, no_identity, zero_workspaces, transient, or
        connection_rejected. Failed lookups are not cached. A fallback identity
        may still yield usable workspaces after a partial failure; those refs
        remain available to fresh turns but not authoritative for replay.
        """
        cache_key = (id(datarc_client), user_id)
        ent = self._cache.get(cache_key)
        if ent is not None and ent.fresh() and not refresh:
            _metric("cache_hit")
            return ent.refs, ent.reason
        self._cache = {key: entry for key, entry in self._cache.items() if entry.fresh() and key != cache_key}
        refs: tuple[DatarcWorkspaceRef, ...] = ()
        account_uid = ""
        if datarc_client is not None:
            account_uid, failure_reason = await self._resolve_identity(datarc_client, user_id)
            if not account_uid and failure_reason:
                # review P2: transient identity resolution failure (Datarc blip): no negative cache; retry next turn,
                # like list_account_projects failure. Never persistently conclude that the user has no workspaces.
                logger.warning("tool_context.identity_transient_fail", user_id=user_id)
                _metric("identity_error")
                return (), failure_reason
            if account_uid:
                try:
                    projects = await datarc_client.list_account_projects(account_uid, limit=200)
                    refs = tuple(
                        DatarcWorkspaceRef(
                            id=did,
                            name=str(p.get("name") or f"工作区 {did}"),
                            desc=str(p.get("desc") or p.get("description") or "")[:120],
                        )
                        for p in (projects or [])
                        if (did := str(p.get("id") or p.get("projectId") or ""))
                    )
                except Exception as e:
                    # Fetch failure does not mean no workspaces: no positive/negative cache (retry next turn). Return empty for
                    # upstream transient-failure handling; never turn a Datarc outage into a cached no-workspaces result for 5min.
                    logger.warning(
                        "tool_context.list_projects_failed",
                        user_id=user_id,
                        err=str(e)[:120],
                    )
                    _metric("list_error")
                    from bowhead_bi_bridge import BIPermissionError

                    reason = "connection_rejected" if isinstance(e, BIPermissionError) and e.source == "http" else "transient"
                    return (), reason
                if failure_reason:
                    # A lower identity tier may still provide usable refs after
                    # an upper-tier read failed.  Fresh turns may use them, but
                    # replay cannot prove the original identity snapshot and no
                    # positive/negative result may be cached as authoritative.
                    logger.warning("tool_context.identity_partial", user_id=user_id)
                    _metric("identity_error")
                    return refs, "transient"
        reason = "" if refs else ("no_identity" if not account_uid else "zero_workspaces")
        self._cache[cache_key] = _CacheEntry(refs=refs, account_uid=account_uid, reason=reason, client=datarc_client)
        _metric("resolved" if refs else reason)
        return refs, reason

    def invalidate_user(self, user_id: str) -> None:
        """手动清缓存——工具面板「重新检测」（PRD §3.2 负缓存工单面）+ 撤权即时生效通道."""
        for key in tuple(self._cache):
            if key[1] == user_id:
                self._cache.pop(key, None)
