"""Permission-scoped ToolSpec instances for curated Datarc connections."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from typing import Any

from bowhead_router.types import ToolSchema

from bowhead_bi_bridge import DatarcClient
from bowhead_core.builtin_datarc_scope import supported_datarc_operations
from bowhead_core.tool_registry import TeamToolExecutionBinding, ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_core.types import ToolAnnotations

logger = logging.getLogger(__name__)

_OPERATION_ORDER = ("datarc_workspaces", "datarc_schema", "datarc_query")


def datarc_tool_ref(tool_id: str, operation: str = "datarc_query") -> str:
    return f"teammcp.{tool_id}.{operation}"


def datarc_source_label(name: str) -> str:
    return f"Datarc - {name}"


def _safe_workspaces(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"id": item["id"], "name": item["name"]} for item in items]


async def _accessible_workspaces(client: DatarcClient, account_uid: str, scope: dict[str, Any]) -> list[dict[str, str]]:
    member_ids: set[str] = set()
    for offset in range(0, 1000, 100):
        rows = await client.list_account_projects(account_uid, offset=offset, limit=100)
        member_ids.update(str(item.get("id") or "") for item in rows if isinstance(item, dict))
        if len(rows) < 100:
            break
    return [
        {"id": str(row["id"]), "name": str(row.get("name") or row["id"])}
        for row in scope.get("workspaces") or []
        if isinstance(row, dict) and str(row.get("id") or "") in member_ids
    ]


def _select_workspace(items: list[dict[str, str]], requested: str) -> tuple[dict[str, str] | None, str | None]:
    if not items:
        return None, "no_accessible_workspaces"
    if not requested.strip():
        return (items[0], None) if len(items) == 1 else (None, "workspace_selection_required")
    needle = requested.strip().casefold()
    matches = [item for item in items if item["id"].casefold() == needle or item["name"].casefold() == needle]
    if len(matches) == 1:
        return matches[0], None
    return None, "workspace_not_found" if not matches else "workspace_selection_ambiguous"


def _operation_schema(operation: str, tool_ref: str, label: str) -> ToolSchema:
    workspace = {"type": "string", "maxLength": 120, "description": "可选的北极九章工作区名称或 ID"}
    if operation == "datarc_workspaces":
        return ToolSchema(
            name=tool_ref,
            description=f"列出当前用户可通过“{label}”访问的北极九章工作区。无需参数。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )
    if operation == "datarc_schema":
        return ToolSchema(
            name=tool_ref,
            description=f"查看“{label}”中工作区的可查询指标和维度。仅有一个可访问工作区时自动选择。",
            input_schema={"type": "object", "properties": {"workspace": workspace}, "additionalProperties": False},
        )
    return ToolSchema(
        name=tool_ref,
        description=(
            f"使用“{label}”查询北极九章业务数据。问题使用自然语言；仅有一个可访问工作区时自动选择，多个工作区时按返回候选重新指定 workspace。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 4, "maxLength": 2000, "description": "自然语言数据问题"},
                "workspace": workspace,
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    )


def build_builtin_datarc_specs(*, team_id: str, tools: list[Any], store: Any, base_registry: ToolRegistry) -> list[ToolSpec]:
    """Expose only operations allowed by each key's last verified scope."""

    enabled_operations = set().union(*(supported_datarc_operations(tool) for tool in tools if tool.provider == "datarc" and tool.status == "enabled"))
    delegates = {
        operation: base_registry.get(base_name)
        for operation, base_name in (("datarc_schema", "bi_schema"), ("datarc_query", "bi_query"))
        if operation in enabled_operations
    }
    specs: list[ToolSpec] = []
    for tool in tools:
        if tool.provider != "datarc" or tool.status != "enabled":
            continue
        supported = supported_datarc_operations(tool)
        label = datarc_source_label(tool.display_name)
        for operation in _OPERATION_ORDER:
            if operation not in supported:
                continue
            tool_ref = datarc_tool_ref(tool.id, operation)

            async def _execute(
                args: dict[str, Any],
                ctx: ToolExecutionContext,
                *,
                _tool: Any = tool,
                _operation: str = operation,
                _tool_ref: str = tool_ref,
            ) -> Any:
                started = time.perf_counter()
                status = "error"
                error_kind: str | None = None
                try:
                    if ctx.team_id != team_id:
                        error_kind = "team_scope_mismatch"
                        return {"ok": False, "error_kind": error_kind}
                    user = await store.get_user(ctx.user_id)
                    if user is None or not user.email or not await store.user_has_active_team_membership(ctx.user_id, team_id):
                        error_kind = "datarc_identity_unavailable"
                        return {"ok": False, "error_kind": error_kind}
                    live_tool = await store.get_team_builtin_tool_instance(team_id, _tool.id)
                    if (
                        live_tool is None
                        or live_tool.provider != "datarc"
                        or live_tool.status != "enabled"
                        or _operation not in supported_datarc_operations(live_tool)
                    ):
                        error_kind = "datarc_connection_unavailable"
                        return {"ok": False, "error_kind": error_kind}
                    auth = await store.get_team_builtin_tool_instance_auth_decrypted(team_id, _tool.id)
                    connection_key = str(auth.get("connection_key") or "")
                    if not connection_key:
                        error_kind = "datarc_connection_unavailable"
                        return {"ok": False, "error_kind": error_kind}
                    client = DatarcClient(base_url=live_tool.endpoint, auth_key=connection_key, ssrf_guard=True)
                    try:
                        account = await client.fetch_account(user.email)
                        account_uid = str((account or {}).get("uid") or "")
                        if not account_uid:
                            error_kind = "datarc_identity_not_found"
                            return {"ok": False, "error_kind": error_kind}
                        workspaces = await _accessible_workspaces(client, account_uid, live_tool.scope)
                        if _operation == "datarc_workspaces":
                            status = "ok"
                            return {"ok": True, "connection": datarc_source_label(live_tool.display_name), "workspaces": workspaces}
                        workspace, selection_error = _select_workspace(workspaces, str(args.get("workspace") or ""))
                        if workspace is None:
                            error_kind = selection_error
                            return {"ok": False, "error_kind": selection_error, "workspaces": _safe_workspaces(workspaces)}
                        bound_ctx = copy.copy(ctx)
                        bound_ctx.datarc = client
                        bound_ctx.active_datarc_project = workspace["id"]
                        bound_ctx.active_datarc_workspace_name = workspace["name"]
                        bound_ctx.accessible_datarc_workspaces = tuple(_safe_workspaces(workspaces))
                        if _operation == "datarc_query":
                            member = await client.fetch_workspace_member(workspace["id"], user.email)
                            member_uid = str((member or {}).get("uid") or "")
                            if not member_uid:
                                error_kind = "datarc_workspace_membership_not_found"
                                return {"ok": False, "error_kind": error_kind, "workspace": workspace}
                            bound_ctx.member_uid = member_uid
                            delegate_args = {"queries": [str(args.get("question") or "").strip()]}
                        else:
                            delegate_args = {}
                        result = await delegates[_operation].handler(delegate_args, bound_ctx)
                        status = "ok"
                        return {
                            "ok": True,
                            "connection": datarc_source_label(live_tool.display_name),
                            "workspace": workspace,
                            "result": result,
                        }
                    finally:
                        await client.aclose()
                except Exception as exc:  # upstream details remain server-side
                    logger.warning("builtin_datarc.call_failed operation=%s err=%s", _operation, type(exc).__name__)
                    error_kind = type(exc).__name__
                    return {"ok": False, "error_kind": "datarc_operation_failed"}
                finally:
                    digest = hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
                    try:
                        await store.log_tool_call(
                            team_id=team_id,
                            user_id=ctx.user_id,
                            source_id=_tool.id,
                            tool_name=_tool_ref,
                            args_digest=digest,
                            status=status,
                            error_kind=error_kind,
                            duration_ms=int((time.perf_counter() - started) * 1000),
                        )
                    except Exception as audit_exc:
                        logger.warning("builtin_datarc.audit_failed team=%s tool=%s err=%s", team_id, _tool_ref, type(audit_exc).__name__)

            specs.append(
                ToolSpec(
                    schema=_operation_schema(operation, tool_ref, label),
                    handler=_execute,
                    allowed_for_roles=frozenset({"analyst"}),
                    risk_level="low",
                    source="dataset",
                    annotations=ToolAnnotations(open_world=False, destructive=False, read_only=True),
                    team_tool_binding=TeamToolExecutionBinding(
                        team_id=team_id,
                        source_id=tool.id,
                        operation_id=operation,
                        advertised_name=tool_ref,
                    ),
                )
            )
    return specs


__all__ = ["build_builtin_datarc_specs", "datarc_source_label", "datarc_tool_ref"]
