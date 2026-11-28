"""Current KB access checks shared by long-running chat and matrix operations."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from typing import TYPE_CHECKING, Any

from bowhead_core._db import open_pooled_read_ctx
from bowhead_core.access_control import AccessContext, normalize_workspace_role, resource_grants_enforce_enabled
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
from bowhead_core.kb.knowledge_base_access import KnowledgeCaller, KnowledgeLevels, TeamPrincipals, named_knowledge_base_id
from bowhead_core.kb.knowledge_base_store import knowledge_bases_enabled
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.schemas import KbFileDTO

from bowhead_gateway.access_context import AccessContextError, SpaceOperation, access_context_to_dict, build_access_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite
    from bowhead_core.kb.files_store import KbFileAccessRow

logger = logging.getLogger(__name__)


def knowledge_caller(ctx: AccessContext) -> KnowledgeCaller:
    """The resolver's view of one access context."""
    return KnowledgeCaller(
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        space_id=ctx.workspace_id or "",
        space_role=(normalize_workspace_role(ctx.workspace_role) or "") if ctx.workspace_id else "",
        is_team_admin=bool(ctx.is_team_admin),
        group_ids=frozenset(ctx.group_ids),
    )


async def knowledge_levels_for(ctx: AccessContext, *, files_store: Any, user_store: Any = None, access_store: Any = None) -> KnowledgeLevels | None:
    """The caller's knowledge base levels in one access context; None when they cannot be read.

    None refuses files in named Space/Team knowledge bases and leaves files in default
    knowledge bases to today's rule, so a failure here never widens access. The caller's
    Spaces and Team-wide groups are read from core only when a Team knowledge base lists
    Spaces or groups; when either read fails, those entries do not apply. With the knowledge
    base switch off, named knowledge bases are hidden (C3): their files are refused and
    member entries raise nobody's level.
    """
    resolve = getattr(files_store, "resolve_knowledge_levels", None)
    if not callable(resolve) or not ctx.user_id or not ctx.team_id:
        return None

    async def team_principals() -> TeamPrincipals:
        spaces: frozenset[str] = frozenset()
        list_spaces = getattr(user_store, "list_workspaces_for_user", None)
        if callable(list_spaces):
            try:
                spaces = frozenset(str(row["workspace_id"]) for row in await list_spaces(ctx.user_id, team_id=ctx.team_id))
            except Exception as exc:
                logger.warning("kb.knowledge_levels_spaces_unavailable user=%s team=%s err=%s", ctx.user_id, ctx.team_id, type(exc).__name__)
        groups = frozenset(ctx.group_ids) if not ctx.workspace_id else frozenset()
        group_scopes = getattr(access_store, "principal_group_scopes", None)
        if callable(group_scopes):
            try:
                groups = frozenset(group for group, workspace in await group_scopes(ctx.team_id, ctx.user_id) if workspace == "")
            except Exception as exc:
                logger.warning("kb.knowledge_levels_groups_unavailable user=%s team=%s err=%s", ctx.user_id, ctx.team_id, type(exc).__name__)
        return TeamPrincipals(space_ids=spaces, team_group_ids=groups)

    try:
        levels = await resolve(knowledge_caller(ctx), load_team_principals=team_principals, named=knowledge_bases_enabled())
    except Exception as exc:
        logger.warning("kb.knowledge_levels_unavailable user=%s team=%s err=%s", ctx.user_id, ctx.team_id, type(exc).__name__)
        return None
    return levels if isinstance(levels, KnowledgeLevels) else None


async def kb_file_decisions(
    files: Sequence[KbFileDTO | KbFileAccessRow],
    ctx: AccessContext,
    access_store: Any,
    *,
    write: bool = False,
    connection: aiosqlite.Connection | None = None,
    files_store: Any = None,
    user_store: Any = None,
) -> dict[str, bool]:
    """Decide each file for one current access context.

    A file passes when it is active, belongs to the context's Team and its scope rule
    (``KbPermission.can_read``, plus ``can_delete`` for a write) admits the caller; when
    resource grants are enforced, its ``kb_file`` and ``kb_scope`` grants must allow it
    too. Raises when the grant decisions cannot be read. The caller's knowledge base levels
    are resolved through ``files_store`` only when a file is in a named knowledge base, or for
    a write while knowledge bases are on (member entries on a default can raise write rights);
    without ``files_store`` named files are refused and defaults keep today's rule.
    """
    read_options = {"connection": connection} if connection is not None else {}
    decisions = {file.file_id: False for file in files}
    scope = ScopeContext(
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        workspace_id=ctx.workspace_id,
        role="team_admin" if ctx.is_team_admin else "ws_admin" if ctx.workspace_role == "ws_admin" else "user",
        space_viewer=ctx.workspace_role == "viewer",
    )
    in_team = [file for file in files if not file.soft_deleted_at and file.team_id == ctx.team_id]
    if files_store is not None and in_team and (any(named_knowledge_base_id(file) for file in in_team) or (write and knowledge_bases_enabled())):
        scope.knowledge_levels = await knowledge_levels_for(ctx, files_store=files_store, user_store=user_store, access_store=access_store)
    eligible = [
        file
        for file in files
        if not file.soft_deleted_at
        and file.team_id == ctx.team_id
        and KbPermission.can_read(file, scope)
        and (not write or KbPermission.can_delete(file, scope, scope.role))
    ]
    if not resource_grants_enforce_enabled():
        return {**decisions, **{file.file_id: True for file in eligible}}
    if access_store is None:
        return decisions
    resources = {
        file.file_id: (
            f"team:{file.scope_id}" if file.scope == "team" else f"workspace:{file.scope_id}" if file.scope in {"space", "workspace"} else None
        )
        for file in eligible
    }
    permission = "write" if write else "read"
    raw = access_context_to_dict(ctx)
    file_decisions = await access_store.explain_decisions(raw, "kb_file", list(resources), permission, **read_options)
    scope_decisions = await access_store.explain_decisions(
        raw, "kb_scope", list(dict.fromkeys(value for value in resources.values() if value)), permission, **read_options
    )
    for identity, resource in resources.items():
        decisions[identity] = bool(
            file_decisions.get(identity, {}).get("allowed") and (resource is None or scope_decisions.get(resource, {}).get("allowed"))
        )
    return decisions


async def readable_kb_file_ids(files_store: Any, ctx: AccessContext, access_store: Any, *, user_store: Any = None) -> tuple[str, ...] | None:
    """The active KB files the caller can read in ``ctx``'s Space, for record and row ceilings."""
    from bowhead_core.kb.permission import readable_file_ids

    return await readable_file_ids(
        files_store,
        lambda files: kb_file_decisions(files, ctx, access_store, files_store=files_store, user_store=user_store),
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        workspace_id=ctx.workspace_id,
    )


def kb_file_authorizer(
    user_store: Any,
    access_store: Any,
    matrix_store: Any = None,
    *,
    files_store: Any = None,
    _connection: aiosqlite.Connection | None = None,
    _history_context: AccessContext | None = None,
) -> Any:
    """The current KB file check for chat, matrices and source validation.

    ``files_store`` is the knowledge files store: files in named knowledge bases need the
    caller's levels from it and are refused without it.
    """
    read_options = {"connection": _connection} if _connection is not None else {}

    async def access_context(user_id: str, team_id: str, workspace_id: str, *, execute: bool = False, write: bool = False) -> AccessContext:
        if _history_context is not None and not execute and not write:
            if (_history_context.user_id, _history_context.team_id, _history_context.workspace_id) != (user_id, team_id, workspace_id):
                raise ValueError("Knowledge authorization scope changed")
            return _history_context
        return await build_access_context(
            user_store=user_store,
            access_store=access_store,
            user_id=user_id,
            workspace_id=workspace_id,
            allow_missing_workspace=True,
            operation=SpaceOperation.WRITE if write else SpaceOperation.EXECUTE if execute else SpaceOperation.READ_HISTORY,
            expected_team_id=team_id,
            **read_options,
        )

    @asynccontextmanager
    async def history_validation(
        user_id: str, team_id: str, workspace_id: str, *, connection: aiosqlite.Connection | None = None
    ) -> AsyncIterator[Any]:
        """Share one current authority snapshot for one metadata validation only."""
        async with nullcontext(connection) if connection is not None else open_pooled_read_ctx(user_store.db_path) as db:
            if connection is None:
                await db.execute("BEGIN")
            try:
                try:
                    context = await build_access_context(
                        user_store=user_store,
                        access_store=access_store,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        allow_missing_workspace=True,
                        operation=SpaceOperation.READ_HISTORY,
                        expected_team_id=team_id,
                        connection=db,
                    )
                except AccessContextError as exc:
                    code = "knowledge_source_authorization_unavailable" if exc.status_code >= 500 else "knowledge_source_changed"
                    raise KnowledgeAccessError(code) from None
                yield kb_file_authorizer(user_store, access_store, matrix_store, files_store=files_store, _connection=db, _history_context=context)
            finally:
                if connection is None:
                    await db.rollback()

    async def batch(
        files: list[KbFileDTO], user_id: str, team_id: str, workspace_id: str, *, execute: bool = False, write: bool = False
    ) -> dict[str, bool] | None:
        try:
            ctx = await access_context(user_id, team_id, workspace_id, execute=execute, write=write)
            return await kb_file_decisions(
                files, ctx, access_store, write=write, connection=_connection, files_store=files_store, user_store=user_store
            )
        except Exception:
            return None

    async def knowledge_levels(user_id: str, team_id: str, workspace_id: str) -> KnowledgeLevels | None:
        """The caller's knowledge base levels in this context, for ``ScopeContext.knowledge_levels``; None when unavailable."""
        if files_store is None:
            return None
        try:
            ctx = await access_context(user_id, team_id, workspace_id)
        except Exception:
            return None
        return await knowledge_levels_for(ctx, files_store=files_store, user_store=user_store, access_store=access_store)

    async def authorize(file: KbFileDTO, user_id: str, team_id: str, workspace_id: str, *, execute: bool = False, write: bool = False) -> bool:
        result = await batch([file], user_id, team_id, workspace_id, execute=execute, write=write)
        return bool(result and result.get(file.file_id))

    async def navigation(origins: list[dict], user_id: str, team_id: str, workspace_id: str) -> set[str]:
        if matrix_store is None or not workspace_id:
            return set()
        try:
            await access_context(user_id, team_id, workspace_id)
            allowed = set()
            matrices = {}
            for origin in origins:
                proof = origin["matrix"]
                if proof.get("workspace_id") != workspace_id:
                    continue
                identity = proof["matrix_id"]
                if identity not in matrices:
                    matrices[identity] = await matrix_store.get_matrix(identity, workspace_id=workspace_id)
                matrix = matrices[identity]
                if matrix is None or matrix.soft_deleted_at or matrix.team_id != team_id or matrix.structure_seq != proof["structure_seq"]:
                    continue
                if getattr(matrix, "visibility", "space") != "space":
                    continue  # Only-me matrices never publish navigation (S8).
                column = next((column for column in matrix.columns if column["column_id"] == proof["column_id"]), None)
                if not column or column.get("navigation_role", "none") != proof["role"] or column["prompt_version"] != proof["prompt_version"]:
                    continue
                if not any(item.get("file_id") == origin["file_id"] for item in matrix.files):
                    continue
                cell = await matrix_store.get_cell(identity, origin["file_id"], proof["column_id"])
                if (
                    cell
                    and cell.status == "done"
                    and not cell.edited_by_user
                    and cell.seq == proof["cell_seq"]
                    and cell.kb_generation_id == origin["generation_id"]
                    and cell.file_version == origin["source_version"]
                    and cell.prompt_version == proof["prompt_version"]
                ):
                    primary = [value for value in cell.variants if value.get("is_primary")]
                    if len(primary) == 1 and primary[0].get("extracted_by") == "tier1":
                        allowed.add(origin["id"])
            return allowed
        except Exception:
            return set()

    async def categories(definitions: list[dict], user_id: str, team_id: str, workspace_id: str) -> list[dict] | None:
        try:
            ctx = await access_context(user_id, team_id, workspace_id)
            if not resource_grants_enforce_enabled():
                return definitions
            if access_store is None:
                return []
            resources = {
                row["id"]: f"team:{row['scope_id']}"
                if row["scope"] == "team"
                else f"workspace:{row['scope_id']}"
                if row["scope"] == "space"
                else None
                for row in definitions
            }
            decisions = await access_store.explain_decisions(
                access_context_to_dict(ctx), "kb_scope", list(dict.fromkeys(value for value in resources.values() if value)), "read", **read_options
            )
            return [row for row in definitions if resources[row["id"]] is None or decisions.get(resources[row["id"]], {}).get("allowed")]
        except Exception:
            return None

    authorize.categories = categories
    authorize.batch = batch
    authorize.knowledge_levels = knowledge_levels
    authorize.navigation = navigation
    authorize.history_validation = history_validation
    return authorize
