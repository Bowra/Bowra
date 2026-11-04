"""Current KB access checks shared by long-running chat and matrix operations."""

from __future__ import annotations

from typing import Any

from bowhead_core.access_control import resource_grants_enforce_enabled
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.schemas import KbFileDTO

from bowhead_gateway.access_context import SpaceOperation, access_context_to_dict, build_access_context


def kb_file_authorizer(user_store: Any, access_store: Any, matrix_store: Any = None):
    async def batch(
        files: list[KbFileDTO], user_id: str, team_id: str, workspace_id: str, *, execute: bool = False, write: bool = False
    ) -> dict[str, bool] | None:
        decisions = {file.file_id: False for file in files}
        try:
            ctx = await build_access_context(
                user_store=user_store,
                access_store=access_store,
                user_id=user_id,
                workspace_id=workspace_id,
                allow_missing_workspace=True,
                operation=SpaceOperation.WRITE if write else SpaceOperation.EXECUTE if execute else SpaceOperation.READ_HISTORY,
                expected_team_id=team_id,
            )
            scope = ScopeContext(
                user_id=user_id,
                team_id=team_id,
                workspace_id=workspace_id,
                role="team_admin" if ctx.is_team_admin else "ws_admin" if ctx.workspace_role == "ws_admin" else "user",
            )
            eligible = [
                file
                for file in files
                if not file.soft_deleted_at
                and file.team_id == team_id
                and KbPermission.can_read(file, scope)
                and (not write or KbPermission.can_delete(file, scope, scope.role))
            ]
            if not resource_grants_enforce_enabled():
                return {**decisions, **{file.file_id: True for file in eligible}}
            if access_store is None:
                return decisions
            resources = {
                file.file_id: (
                    f"team:{file.scope_id}"
                    if file.scope == "team"
                    else f"workspace:{file.scope_id}"
                    if file.scope in {"space", "workspace"}
                    else None
                )
                for file in eligible
            }
            permission = "write" if write else "read"
            raw = access_context_to_dict(ctx)
            file_decisions = await access_store.explain_decisions(raw, "kb_file", list(resources), permission)
            scope_decisions = await access_store.explain_decisions(
                raw, "kb_scope", list(dict.fromkeys(value for value in resources.values() if value)), permission
            )
            for identity, resource in resources.items():
                decisions[identity] = bool(
                    file_decisions.get(identity, {}).get("allowed") and (resource is None or scope_decisions.get(resource, {}).get("allowed"))
                )
            return decisions
        except Exception:
            return None

    async def authorize(file: KbFileDTO, user_id: str, team_id: str, workspace_id: str, *, execute: bool = False, write: bool = False) -> bool:
        result = await batch([file], user_id, team_id, workspace_id, execute=execute, write=write)
        return bool(result and result.get(file.file_id))

    async def navigation(origins: list[dict], user_id: str, team_id: str, workspace_id: str) -> set[str]:
        if matrix_store is None or not workspace_id:
            return set()
        try:
            await build_access_context(
                user_store=user_store,
                access_store=access_store,
                user_id=user_id,
                workspace_id=workspace_id,
                operation=SpaceOperation.READ_HISTORY,
                expected_team_id=team_id,
            )
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
            ctx = await build_access_context(
                user_store=user_store,
                access_store=access_store,
                user_id=user_id,
                workspace_id=workspace_id,
                allow_missing_workspace=True,
                operation=SpaceOperation.READ_HISTORY,
                expected_team_id=team_id,
            )
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
                access_context_to_dict(ctx), "kb_scope", list(dict.fromkeys(value for value in resources.values() if value)), "read"
            )
            return [row for row in definitions if resources[row["id"]] is None or decisions.get(resources[row["id"]], {}).get("allowed")]
        except Exception:
            return None

    authorize.categories = categories
    authorize.batch = batch
    authorize.navigation = navigation
    return authorize
