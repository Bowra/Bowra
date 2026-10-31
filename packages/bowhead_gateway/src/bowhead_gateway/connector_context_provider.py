"""Native connector selection policy and the shared preference commit adapter.

Datarc uses Team/registry-connection scoped workspace addresses, with an exact
Session overlay ahead of the default. The same ladder serves fresh turns and
Plan recovery. Runtime publication belongs to datarc_selection; this module
reports durable facts and does not broadcast preferences to admitted work.
Generic connectors and the independent account slot retain their legacy keys.
See docs/tool-context-framework.en-US.md for the current contract.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, Protocol

from bowhead_core.datarc_selection import DatarcCommitUnknown, DatarcPersistenceReceipt, DatarcPreferenceScope
from bowhead_core.scrub import scrub

logger = logging.getLogger(__name__)


class ResolvedConnectorContext(NamedTuple):
    """Provider.resolve 的返回：``context_id=None`` = 该 connector 上下文未定 →
    dispatch 闸产 ``tool_context_select`` sentinel（可及集合 >1）或优雅缺席。"""

    context_id: str | None
    context_name: str
    accessible: tuple[dict[str, str], ...]  # (id/name[/desc]) dict snapshot for picker options + provenance.
    # bi-absent-turn R3.1: reason for empty accessible ("" | "no_identity" |
    # "zero_workspaces" | "transient"). Append a defaulted field so all existing ladder constructors
    # retain their three arguments; only the provider injects it via `_replace` for empty sets (unchanged ladder semantics).
    absence_reason: str = ""


class ConnectorContextProvider(Protocol):
    """每 connector 一个的上下文解析契约。

    ``tool`` 属性 = **connector 实例 id 的冻结拼写**（v2 术语迁移说明见模块
    docstring）：同时是存储 key（user_memory tool_context 槽）、弹卡 payload 的
    ``tool`` 字段、``tool_switch_context`` 的 tool 参数——三处共用一个值。
    属性名保留 ``tool``（它是存储 key 的取值来源，改名收益低穿透面广，Q2 拍板）。
    """

    tool: str

    async def accessible(self, user_id: str) -> tuple[Any, ...]:
        """该用户此 connector 的可及上下文集合（元素须有 .id/.name，可选 .desc）。"""
        ...

    async def resolve(
        self,
        user_id: str,
        *,
        session_id: str | None,
        read_only: bool = False,
    ) -> ResolvedConnectorContext:
        """定「这个 turn 用哪个上下文」。策略自便；默认用 DefaultContextLadder。"""
        ...


class ScopedDatarcMemory:
    """Adapt the original ladder to exact workspace preference addresses."""

    def __init__(self, store: Any, scope: DatarcPreferenceScope, session_id: str | None):
        self.store, self.scope, self.session_id = store, scope, session_id

    async def get_tool_context(self, user_id: str, tool: str, *, session_scoped: bool = False):
        if self.store is None or (session_scoped and not self.session_id):
            return None
        return await self.store.get_datarc_workspace(user_id, self.scope, session_id=self.session_id if session_scoped else None)

    async def set_tool_context(self, user_id: str, tool: str, **kwargs):
        return await self.store.commit_datarc_workspace(
            user_id,
            self.scope,
            context_id=kwargs["context_id"],
            context_name=kwargs.get("context_name", ""),
            source=kwargs.get("source"),
            remember=True,
            session_id=None,
        )

    async def clear_tool_context(self, user_id: str, tool: str):
        await self.store.clear_datarc_workspace(user_id, self.scope)


class DefaultContextLadder:
    """默认策略：批2 PRD §2.1 阶梯，语义与原 datarc 实现逐条保真（零行为变化）.

    档序：
    1. 会话覆盖层（``@session`` key；session_id 匹配才认；指向不可及 → 忽略不清）
    2. 永久默认（base key；source=session 跨会话视同无选择〔BUG1 前存量 back-compat〕；
       集合空信任存量〔datarc 故障不误清，授权真值在 member 解析 fail-closed〕；
       确实不可及 → 清偏好落阶梯；source=auto 且集合>1 → 降级重走）
    3. 集合=1 → 自动选中（写回 source=auto）
    4. None → 下游 sentinel 弹卡 / 优雅缺席
    """

    def __init__(
        self,
        *,
        tool: str,
        umem: Any,
        log: logging.Logger | None = None,
        preference_scope: DatarcPreferenceScope | None = None,
    ) -> None:
        self.preference_scope = preference_scope
        self.tool = tool
        self._umem = umem
        self._log = log or logger

    async def resolve(
        self,
        user_id: str,
        *,
        session_id: str | None,
        accessible: tuple[Any, ...],
        read_only: bool = False,
    ) -> ResolvedConnectorContext:
        umem = ScopedDatarcMemory(self._umem, self.preference_scope, session_id) if self.preference_scope else self._umem
        tool = self.tool
        _log = self._log
        refs = accessible
        ref_dicts = tuple(r.as_dict() for r in refs)
        ids = {r.id for r in refs}

        # ── Tier 1: session overlay (switch to XX / do not remember), highest priority within this session ──
        try:
            sess = await umem.get_tool_context(user_id, tool, session_scoped=True)
        except Exception as exc:
            _log.warning("tool_context.get_session_failed user=%s err=%s", user_id, scrub(str(exc)))
            if read_only:
                raise
            sess = None
        if sess and sess.get("id") and str(sess.get("session_id") or "") == str(session_id or ""):
            sid = str(sess["id"])
            if not refs:  # Empty set (upstream failure) -> trust saved context.
                return ResolvedConnectorContext(sid, str(sess.get("name") or ""), ref_dicts)
            if sid in ids:
                name = next((r.name for r in refs if r.id == sid), str(sess.get("name") or ""))
                return ResolvedConnectorContext(sid, name, ref_dicts)
            # Inaccessible overlay -> ignore it (do not clear; it expires across sessions), then read the permanent default.

        # ── Tier 2: permanent default (explicit/auto/last_used) + validity check ──
        chosen: dict[str, Any] | None = None
        cid = ""
        source = ""
        try:
            chosen = await umem.get_tool_context(user_id, tool)
        except Exception as exc:
            _log.warning("tool_context.get_failed user=%s err=%s", user_id, scrub(str(exc)))
            if read_only:
                raise
        if chosen and chosen.get("id"):
            cid = str(chosen["id"])
            source = str(chosen.get("source") or "")
            # Backward compatibility: pre-BUG1 base-key rows may have source=session; retain session
            # semantics (no choice across sessions; never automatically revive an old explicit selection).
            if source == "session" and str(chosen.get("session_id") or "") != str(session_id or ""):
                chosen = None
                cid = ""
        if chosen and cid:
            if not refs:
                # Empty set (no context or upstream failure) -> trust the saved choice (see class docstring).
                return ResolvedConnectorContext(cid, str(chosen.get("name") or ""), ref_dicts)
            if cid in ids:
                if source == "auto" and len(refs) > 1:
                    # Auto choice + expanded set -> fall back through the ladder (keep row until user confirmation overwrites it).
                    return ResolvedConnectorContext(None, "", ref_dicts)
                name = next((r.name for r in refs if r.id == cid), str(chosen.get("name") or ""))
                return ResolvedConnectorContext(cid, name, ref_dicts)
            # Truly inaccessible (revoked/deleted) -> clear preference and descend the ladder.
            if not read_only:
                try:
                    await umem.clear_tool_context(user_id, tool)
                except Exception as exc:
                    _log.warning("tool_context.clear_failed user=%s err=%s", user_id, scrub(str(exc)))
            _log.info("tool_context.choice_revoked user=%s ws=%s", user_id, cid)

        # ── Tier 3: one-item set selects automatically ──
        if len(refs) == 1 and not read_only:
            try:
                await umem.set_tool_context(
                    user_id,
                    tool,
                    context_id=refs[0].id,
                    context_name=refs[0].name,
                    source="auto",
                )
            except Exception as exc:
                _log.warning("tool_context.auto_set_failed user=%s err=%s", user_id, scrub(str(exc)))
        if len(refs) == 1:
            return ResolvedConnectorContext(refs[0].id, refs[0].name, ref_dicts)

        # ── Tier 4 (>1 without choice / =0): dispatch-gate sentinel / graceful absence ──
        return ResolvedConnectorContext(None, "", ref_dicts)


async def commit_connector_context_choice(
    umem: Any,
    *,
    user_id: str,
    tool: str,
    context_id: str,
    context_name: str = "",
    remember: bool,
    session_id: str | None,
    preference_scope: DatarcPreferenceScope | None = None,
    log: logging.Logger | None = None,
) -> DatarcPersistenceReceipt:
    """The switch and picker share one scoped, atomic preference transaction."""
    destination = "default" if remember else "session"
    if tool != "datarc":
        if umem is None:
            return DatarcPersistenceReceipt("skipped", destination, "missing_store")
        try:
            await umem.set_tool_context(
                user_id,
                tool,
                context_id=context_id,
                context_name=context_name,
                source="explicit" if remember else "session",
                session_id=None if remember else str(session_id or ""),
            )
            if remember:
                await umem.clear_tool_context(user_id, tool, session_scoped=True)
        except Exception as error:
            (log or logger).warning("tool_context.commit_failed user=%s tool=%s error=%s", user_id, tool, type(error).__name__)
            return DatarcPersistenceReceipt("failed", destination, "storage_failed")
        return DatarcPersistenceReceipt("saved", destination)
    if umem is None or preference_scope is None:
        return DatarcPersistenceReceipt("skipped", destination, "missing_store" if umem is None else "missing_scope")
    try:
        return await umem.commit_datarc_workspace(
            user_id,
            preference_scope,
            context_id=context_id,
            context_name=context_name,
            remember=remember,
            session_id=session_id,
        )
    except DatarcCommitUnknown:
        return DatarcPersistenceReceipt("unknown", destination, "commit_unconfirmed")
    except Exception as error:
        (log or logger).warning("tool_context.commit_failed user=%s tool=%s error=%s", user_id, tool, type(error).__name__)
        return DatarcPersistenceReceipt("failed", destination, "storage_failed")


class DatarcContextProvider:
    """connector 实例 #1：datarc（北极九章）。per-turn 构造（datarc_client 是 team 级信封成员）.

    ``tool = "datarc"`` 用**类型名**当实例 id——「每 team 单 active 连接」不变量
    （datarc_connection_store 部分唯一索引）下实例↔类型 1:1 的特例；多实例
    connector（teammcp）的 key 必须是 ``ots_*`` 实例 id。
    可及集合走 :class:`~bowhead_gateway.connector_context_service.ConnectorContextService`
    （身份阶梯 L1/L3 + 正负缓存在 service 内）；策略 = DefaultContextLadder。
    """

    tool = "datarc"

    def __init__(
        self,
        *,
        service: Any,
        umem: Any,
        datarc_client: Any,
        log: logging.Logger | None = None,
        preference_scope: DatarcPreferenceScope | None = None,
    ) -> None:
        self._service = service
        self._datarc_client = datarc_client
        self._ladder = DefaultContextLadder(tool=self.tool, umem=umem, log=log, preference_scope=preference_scope)

    async def accessible(self, user_id: str) -> tuple[Any, ...]:
        return await self._service.accessible_workspaces(self._datarc_client, user_id)  # type: ignore[no-any-return]

    async def resolve(
        self,
        user_id: str,
        *,
        session_id: str | None,
        read_only: bool = False,
    ) -> ResolvedConnectorContext:
        # bi-absent-turn R3.1: use _ex for the empty-set reason; inject via `_replace` when the ladder result has an empty set.
        # The ladder and its 9 constructors are unchanged (ladder semantics are outside sprint scope). Its
        # empty-set/trust-saved tier may return a nonempty context_id + empty accessible, which is not absence.
        # The reason matters only when context_id is also None, but pass it unchanged to the consumer
        # (the dispatch gate reads it only when project resolution fails).
        refs, reason = await self._service.accessible_workspaces_ex(self._datarc_client, user_id)
        if read_only and reason in {"transient", "connection_rejected"}:
            raise RuntimeError("Datarc context snapshot is unavailable")
        resolved = await self._ladder.resolve(
            user_id,
            session_id=session_id,
            accessible=refs,
            read_only=read_only,
        )
        if reason and not resolved.accessible:
            return resolved._replace(absence_reason=reason)
        return resolved
