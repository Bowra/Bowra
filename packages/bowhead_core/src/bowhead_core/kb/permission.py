"""KB permission + ScopeContext.

Tech design v3.6 §3.7. round 6 P0-R1: can_write_to_scope 必验 scope_id 跟 ctx 匹配.
round 5 P0-Q1: can_change_scope method 加.
round 5 P1-Q4: personal cross-user promote 禁.
round 5 P1-O5: ScopeContext 加 role 字段.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bowhead_core.kb.schemas import KbFileDTO

Role = Literal["user", "ws_admin", "team_admin"]

_search_logger = logging.getLogger("bowhead_core.kb.search")


@dataclass
class ScopeContext:
    """Caller 的 scope 上下文 (从 JWT auth middleware 解出)."""

    user_id: str
    workspace_id: str
    team_id: str
    role: Role = "user"  # P1-O5: real auth JWT roles; V1 mock BOWHEAD_AUTO_ADMIN_DOMAINS env.
    # 0.40.4 Bug 8 step a: workspaces where this user has personal-scope chunks.
    # Personal chunks live in `kb_ws_{workspace_id_at_upload}` collections, but
    # the user's CURRENT session workspace may not match. Caller (kb_search tool)
    # queries kb_files_store.list_distinct_workspaces_for_user(user_id) and
    # passes the result so KbStore.search can query those collections too.
    # Datarc ws ≠ Bowhead KB scope per user feedback.
    extra_personal_workspaces: list[str] = field(default_factory=list)


class KbPermission:
    """Permission checks for KB operations.

    跟 V4 PRD-D-4-4 admin scope 同模式. 所有方法 staticmethod, 无状态.
    """

    @staticmethod
    def owner_of(file: KbFileDTO) -> str:
        """文件的归属人。个人文件 = ``scope_id``（与列表、检索的 ``scope_owner`` 同一列）；
        其余范围 = 上传者。

        管理员把别人的文件改到自己的个人范围后 ``scope_id=管理员``、``uploaded_by`` 仍是原上传者：
        列表与检索按 ``scope_id`` 走，读/删/下载若按 ``uploaded_by`` 判就会出现「列得出却打不开、
        原上传者反而还能下载」。个人文件的一切归属判断统一按 ``scope_id``。
        """
        if file.scope == "personal":
            return file.scope_id
        return file.uploaded_by

    @staticmethod
    def can_read(file: KbFileDTO, ctx: ScopeContext) -> bool:
        """Read = 看 file metadata + chunks + preview + download.

        - personal: 仅归属人（scope_id）自己
        - workspace: ws 任何 member
        - team: team 任何 member (ws_admin / team_admin / user 都行)
        """
        if file.scope == "personal":
            return file.scope_id == ctx.user_id
        if file.scope in ("space", "workspace"):  # Batch3/c1 dual read.
            return file.scope_id == ctx.workspace_id
        if file.scope == "team":
            return file.scope_id == ctx.team_id
        return False

    @staticmethod
    def can_write_to_scope(
        scope: str,
        scope_id: str,
        ctx: ScopeContext,
        role: Role,
    ) -> bool:
        """Require the caller's own user, current Space or administered Team.

        Space administration alone does not grant Team-wide write authority.
        """
        if scope == "personal":
            return scope_id == ctx.user_id
        if scope in ("space", "workspace"):  # Batch3/c1 dual read.
            return scope_id == ctx.workspace_id
        if scope == "team":
            return scope_id == ctx.team_id and role == "team_admin"
        return False

    @staticmethod
    def can_delete(file: KbFileDTO, ctx: ScopeContext, role: Role) -> bool:
        """Delete = soft delete (90 day grace 后 hard delete cron).

        - personal: 仅归属人（scope_id）自己
        - workspace: uploader 或 ws_admin / team_admin
        - team: 仅 team_admin
        """
        if file.scope == "personal":
            return file.scope_id == ctx.user_id
        if file.scope in ("space", "workspace"):  # Batch3/c1 dual read.
            return file.uploaded_by == ctx.user_id or role in ("ws_admin", "team_admin")
        if file.scope == "team":
            return role == "team_admin"
        return False

    @staticmethod
    def can_change_scope(
        file: KbFileDTO,
        ctx: ScopeContext,
        new_scope: str,
        new_scope_id: str,
    ) -> bool:
        """[P0-Q1 round 5] Scope change 权限. 必同时满足:

        1. 旧文件 caller 能 delete (拥有权 / admin)
        2. 新 scope + scope_id caller 能 write (P0-R1: 必验 scope_id 跟 ctx 匹配)
        3. [P1-Q4] personal cross-user promote 禁 (除非 admin override 走 reason audit)
        """
        if not KbPermission.can_delete(file, ctx, ctx.role):
            return False
        if not KbPermission.can_write_to_scope(new_scope, new_scope_id, ctx, ctx.role):
            return False
        # P1-Q4: other-user personal document → own personal forbidden (ownership transfer future sprint).
        # Admin exception (audited); hard user-level block.
        return not (new_scope == "personal" and KbPermission.owner_of(file) != ctx.user_id and ctx.role not in ("ws_admin", "team_admin"))


async def drop_chunks_of_deleted_files(files_store: Any | None, chunks: list[Any]) -> list[Any]:
    """检索结果兜底：丢掉文件行已软删的分块（对话 kb_search 与页面深搜共用）。

    正常情况下删除会把分块标为 inactive，检索本就过滤；这里防的是删除与解析写入交错等
    漏网情况。只按 ``files_store.get`` 的 ``soft_deleted_at`` 判；查不到文件行或查询失败
    时保留分块（兜底层不因存储抖动吞掉正常结果），并记 warning。
    """
    if files_store is None or not chunks:
        return chunks
    deleted: dict[str, bool] = {}
    for chunk in chunks:
        file_id = str(getattr(chunk, "file_id", "") or "")
        if not file_id or file_id in deleted:
            continue
        try:
            row = await files_store.get(file_id)
        except Exception as exc:
            _search_logger.warning("kb_search.file_lookup_failed file=%s err=%s", file_id, exc)
            row = None
        deleted[file_id] = bool(row is not None and getattr(row, "soft_deleted_at", None))
    return [c for c in chunks if not deleted.get(str(getattr(c, "file_id", "") or ""), False)]


async def resolve_extra_personal_workspaces(files_store: Any | None, user_id: str) -> list[str]:
    """KB 检索范围解析的唯一入口：列出 caller 自己个人文件所在的全部空间（含历史空串空间）。

    对话检索工具（kb_search）与页面深搜（POST /kb/search）共用，保证两处看到同一批个人文件
    （知识库体验整治 PRD FR6.1）。结果只用于 ``ScopeContext.extra_personal_workspaces``：
    ``KbStore.search`` 对这些集合只套 ``scope=personal AND scope_owner=<caller>`` 过滤，
    不会带出别人的个人分块或其他空间的空间分块。

    Best-effort：files_store 缺失或查询失败时返回空列表（退回只查当前空间），并记 warning。
    """
    if files_store is None or not user_id:
        return []
    try:
        return list(await files_store.list_distinct_workspaces_for_user(user_id))
    except Exception as exc:
        # Warn-only — legacy single-ws path still works. Logging the actual
        # exception so prod DB schema drift / lock contention doesn't silently
        # degrade KB search to ws=session-only.
        _search_logger.warning(
            "kb_search.list_distinct_workspaces_failed user_id=%s err=%s",
            user_id,
            exc,
        )
        return []
