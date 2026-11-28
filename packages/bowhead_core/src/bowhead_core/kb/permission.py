"""KB permission + ScopeContext.

Tech design v3.6 §3.7. round 6 P0-R1: can_write_to_scope 必验 scope_id 跟 ctx 匹配.
round 5 P0-Q1: can_change_scope method 加.
round 5 P1-Q4: personal cross-user promote 禁.
round 5 P1-O5: ScopeContext 加 role 字段.

Knowledge bases (phase 1 §2.2, fail-closed rule):

- Personal files: owner only, whatever their knowledge base.
- Space and Team files in their home's default knowledge base (a NULL or the deterministic
  default ID, recognized without a read): today's scope rule exactly. C2 holds through it:
  Team owners/admins keep their Space rights.
- Space and Team files in a named knowledge base: the caller's level from
  ``ScopeContext.knowledge_levels`` decides: use to read; edit to add or to remove/move
  your own files; manage for others' files. Without levels (a call site that does not
  resolve them, or the knowledge base switch off) such files are refused.
- Member entries on a home's default can raise write rights (C6): adding needs edit on the
  default, removing your own files edit and others' files manage, as for named ones. This is
  added to today's rule, never instead of it, so without entries (or levels) nothing changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.kb.knowledge_base_access import KNOWLEDGE_EDIT, KNOWLEDGE_MANAGE, KNOWLEDGE_USE, named_knowledge_base_id
from bowhead_core.kb.knowledge_base_store import default_for_scope, home_for_scope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from bowhead_core.kb.files_store import KbFileAccessRow
    from bowhead_core.kb.knowledge_base_access import KnowledgeBaseSearch, KnowledgeBaseSelection
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
    # The caller's current Space role is viewer (read-only). Viewers can read and
    # search Space knowledge but cannot add, change or remove Space files; their
    # personal files are unaffected. Team admins keep their Team-level rights.
    space_viewer: bool = False
    # {knowledge_base_id: level} from `resolve_knowledge_levels` for this context (My home,
    # current Space, Team). None when not resolved: files in named Space/Team knowledge
    # bases are then refused (fail-closed); default knowledge bases never need it.
    knowledge_levels: Mapping[str, int] | None = None
    # Restricts a search to one knowledge base the caller can use (the per-knowledge-base deep
    # search); None searches every knowledge base of the context as before.
    knowledge_base_search: KnowledgeBaseSearch | None = None
    # An Agent's selected knowledge bases the caller can use, per home: the search pre-filter of the
    # selected-knowledge-base ceiling. None searches every knowledge base of the context as before.
    knowledge_base_selection: KnowledgeBaseSelection | None = None


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
            in_scope = file.scope_id == ctx.workspace_id
        elif file.scope == "team":
            in_scope = file.scope_id == ctx.team_id
        else:
            return False
        level = _named_level(file, ctx)
        return in_scope and (level is None or level >= KNOWLEDGE_USE)

    @staticmethod
    def can_write_to_scope(
        scope: str,
        scope_id: str,
        ctx: ScopeContext,
        role: Role,
        *,
        knowledge_base_id: str | None = None,
    ) -> bool:
        """Require the caller's own user, current Space or administered Team.

        Space administration alone does not grant Team-wide write authority. A named target
        knowledge base (``knowledge_base_id`` other than the scope's default) needs edit on it.
        """
        if scope == "personal":
            in_scope = scope_id == ctx.user_id
        elif scope in ("space", "workspace"):  # Batch3/c1 dual read.
            in_scope = scope_id == ctx.workspace_id
        elif scope == "team":
            in_scope = scope_id == ctx.team_id
        else:
            return False
        default = default_for_scope(scope, scope_id, ctx.team_id)
        if knowledge_base_id and knowledge_base_id != default:
            return in_scope and _level_in_home(knowledge_base_id, home_for_scope(scope, scope_id), ctx) >= KNOWLEDGE_EDIT
        if scope in ("space", "workspace"):
            allowed = not _read_only_in_space(ctx, role)
        elif scope == "team":
            allowed = role == "team_admin"
        else:
            allowed = True
        # C6: an entry on the home's default can raise the right to add, never lower it.
        return in_scope and (allowed or _level_in_home(default, home_for_scope(scope, scope_id), ctx) >= KNOWLEDGE_EDIT)

    @staticmethod
    def can_delete(file: KbFileDTO, ctx: ScopeContext, role: Role) -> bool:
        """Delete = soft delete (90 day grace 后 hard delete cron).

        - personal: 仅归属人（scope_id）自己
        - workspace: uploader 或 ws_admin / team_admin
        - team: 仅 team_admin
        """
        if file.scope == "personal":
            return file.scope_id == ctx.user_id
        level = _named_level(file, ctx)
        if level is not None:
            # Named knowledge base: manage for anyone's files, edit for your own.
            in_scope = file.scope_id == (ctx.team_id if file.scope == "team" else ctx.workspace_id)
            return in_scope and _level_removes(level, file, ctx)
        if file.scope in ("space", "workspace"):  # Batch3/c1 dual read.
            if not _read_only_in_space(ctx, role) and (file.uploaded_by == ctx.user_id or role in ("ws_admin", "team_admin")):
                return True
        elif file.scope == "team":
            if role == "team_admin":
                return True
        else:
            return False
        # C6: an entry on the home's default can raise the right to remove, never lower it.
        in_scope = file.scope_id == (ctx.team_id if file.scope == "team" else ctx.workspace_id)
        return in_scope and _level_removes(_default_level(file, ctx), file, ctx)

    @staticmethod
    def can_change_scope(
        file: KbFileDTO,
        ctx: ScopeContext,
        new_scope: str,
        new_scope_id: str,
        *,
        target_knowledge_base_id: str | None = None,
    ) -> bool:
        """[P0-Q1 round 5] Scope change 权限. 必同时满足:

        1. 旧文件 caller 能 delete (拥有权 / admin)
        2. 新 scope + scope_id caller 能 write (P0-R1: 必验 scope_id 跟 ctx 匹配)
        3. [P1-Q4] personal cross-user promote 禁 (除非 admin override 走 reason audit)

        A move between knowledge bases maps to the same checks: moving your own file needs
        edit where it is and others' files manage (``can_delete``); the target needs edit.
        """
        if not KbPermission.can_delete(file, ctx, ctx.role):
            return False
        if not KbPermission.can_write_to_scope(new_scope, new_scope_id, ctx, ctx.role, knowledge_base_id=target_knowledge_base_id):
            return False
        # P1-Q4: other-user personal document → own personal forbidden (ownership transfer future sprint).
        # Admin exception (audited); hard user-level block.
        return not (new_scope == "personal" and KbPermission.owner_of(file) != ctx.user_id and ctx.role not in ("ws_admin", "team_admin"))

    @staticmethod
    def knowledge_base_level(knowledge_base_id: str, ctx: ScopeContext) -> int:
        """The caller's level on one knowledge base in this context; 0 when none or not resolved."""
        levels = ctx.knowledge_levels
        return int(levels.get(knowledge_base_id, 0)) if levels is not None else 0


def _read_only_in_space(ctx: ScopeContext, role: Role) -> bool:
    """A Space viewer without Team admin rights may not write Space knowledge."""
    return ctx.space_viewer and role != "team_admin"


def _level_in_home(knowledge_base_id: str, home: tuple[str, str], ctx: ScopeContext) -> int:
    """The caller's level on a knowledge base of one home; 0 when unresolved, unusable or in another home."""
    levels = ctx.knowledge_levels
    if levels is None:
        return 0
    home_of = getattr(levels, "home_of", None)
    if callable(home_of) and home_of(knowledge_base_id) not in (None, home):
        return 0
    return int(levels.get(knowledge_base_id, 0))


def _named_level(file: Any, ctx: ScopeContext) -> int | None:
    """None when a Space/Team file is in its home's default (today's rule); else the caller's level there."""
    named = named_knowledge_base_id(file)
    if named is None:
        return None
    return _level_in_home(named, home_for_scope(file.scope, file.scope_id), ctx)


def _default_level(file: Any, ctx: ScopeContext) -> int:
    """The caller's level on the default knowledge base of a Space/Team file's home; 0 when unresolved."""
    if ctx.knowledge_levels is None:
        return 0
    try:
        default = default_for_scope(file.scope, file.scope_id, file.team_id)
    except (AttributeError, TypeError, ValueError):
        return 0
    return _level_in_home(default, home_for_scope(file.scope, file.scope_id), ctx)


def _level_removes(level: int, file: Any, ctx: ScopeContext) -> bool:
    """Manage removes anyone's files, edit only your own."""
    return level >= KNOWLEDGE_MANAGE or (level >= KNOWLEDGE_EDIT and KbPermission.owner_of(file) == ctx.user_id)


async def readable_file_ids(
    files_store: Any,
    decide: Callable[[list[KbFileAccessRow]], Awaitable[dict[str, bool] | None]],
    *,
    user_id: str,
    team_id: str,
    workspace_id: str,
) -> tuple[str, ...] | None:
    """IDs of the active KB files the caller can read in one Space context.

    The candidates are the caller's own personal files, the current Space's files and the
    Team's files, read in one query as access rows. ``decide`` applies the per-file read rule
    (and resource grants when they are enforced) to all of them at once. Extraction records
    and rows are stored under their upload Space whoever owns the file, so record and row
    reads use this set as their file ceiling. Returns None when ``decide`` cannot decide;
    callers fail closed.
    """
    if not team_id:
        return ()
    files = await files_store.list_access_rows_for_scopes(
        team_id=team_id,
        personal_owner_id=user_id or None,
        space_id=workspace_id or None,
        team_scope_id=team_id,
    )
    if not files:
        return ()
    decisions = await decide(files)
    if decisions is None:
        return None
    return tuple(file.file_id for file in files if decisions.get(file.file_id))


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
