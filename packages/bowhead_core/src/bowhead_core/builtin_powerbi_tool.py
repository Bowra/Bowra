"""Per-connection Power BI tools for team Agents."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from typing import Any

from bowhead_router.types import ToolSchema

from bowhead_core.pbi_client import build_pbi_turn_handle
from bowhead_core.pbi_tools import (
    _pbi_execute_dax,
    _pbi_execute_template,
    _pbi_fetch_rows,
    _pbi_list_measures,
    _pbi_list_templates,
)
from bowhead_core.tool_registry import TeamToolExecutionBinding, ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_core.types import ToolAnnotations

logger = logging.getLogger(__name__)

OPERATIONS = ("pbi_execute_dax", "pbi_fetch_rows", "pbi_list_measures", "pbi_list_templates", "pbi_execute_template")


_HANDLERS = {
    "pbi_execute_dax": _pbi_execute_dax,
    "pbi_fetch_rows": _pbi_fetch_rows,
    "pbi_list_measures": _pbi_list_measures,
    "pbi_list_templates": _pbi_list_templates,
    "pbi_execute_template": _pbi_execute_template,
}


def powerbi_tool_ref(tool_id: str, operation: str) -> str:
    return f"teammcp.{tool_id}.{operation}"


def powerbi_source_label(name: str) -> str:
    return f"Power BI - {name}"


def _schema(operation: str, name: str, label: str) -> ToolSchema:
    properties: dict[str, Any] = {}
    required: list[str] = []
    if operation in {"pbi_execute_dax", "pbi_fetch_rows"}:
        properties = {"dax": {"type": "string", "description": "一条 DAX EVALUATE 查询"}}
        required = ["dax"]
    elif operation == "pbi_execute_template":
        properties = {"template_id": {"type": "string"}, "parameters": {"type": "object"}}
        required = ["template_id", "parameters"]
    descriptions = {
        "pbi_execute_dax": "执行只读 DAX 查询",
        "pbi_fetch_rows": "读取明细或聚合行",
        "pbi_list_measures": "查看已配置的度量目录",
        "pbi_list_templates": "列出管理员维护的 DAX 模板",
        "pbi_execute_template": "填写参数并执行 DAX 模板",
    }
    return ToolSchema(
        name=name,
        description=f"使用“{label}”{descriptions[operation]}。",
        input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    )


def build_builtin_powerbi_specs(*, team_id: str, tools: list[Any], store: Any, base_registry: ToolRegistry) -> list[ToolSpec]:
    del base_registry
    specs: list[ToolSpec] = []
    for tool in tools:
        if tool.provider != "powerbi" or tool.status != "enabled":
            continue
        label = powerbi_source_label(tool.display_name)
        for operation in OPERATIONS:
            ref = powerbi_tool_ref(tool.id, operation)

            async def execute(
                args: dict[str, Any],
                ctx: ToolExecutionContext,
                *,
                tool_id: str = tool.id,
                op: str = operation,
                tool_ref: str = ref,
            ) -> Any:
                started = time.perf_counter()
                status = "error"
                error_kind: str | None = None
                try:
                    if ctx.team_id != team_id or not await store.user_has_active_team_membership(ctx.user_id, team_id):
                        error_kind = "team_scope_mismatch"
                        return {"ok": False, "error_kind": error_kind, "error_code": "pbi_connection_unavailable"}
                    live = await store.get_team_builtin_tool_instance(team_id, tool_id)
                    if live is None or live.provider != "powerbi" or live.status != "enabled":
                        error_kind = "pbi_connection_unavailable"
                        return {"ok": False, "error_kind": error_kind, "error_code": error_kind}
                    auth = await store.get_team_builtin_tool_instance_auth_decrypted(team_id, tool_id)
                    if not all(auth.get(key) for key in ("tenant_id", "client_id", "client_secret", "workspace_id", "dataset_id")):
                        error_kind = "pbi_connection_credential_unavailable"
                        return {"ok": False, "error_kind": error_kind, "error_code": error_kind}
                    templates = await store.list_builtin_pbi_templates(team_id, tool_id)
                    bound_ctx = copy.copy(ctx)
                    bound_ctx.pbi = build_pbi_turn_handle(
                        credential=auth,
                        workspace_id=auth["workspace_id"],
                        dataset_id=auth["dataset_id"],
                        catalog={"templates": templates},
                    )
                    result = await _HANDLERS[op](args, bound_ctx)
                    if isinstance(result, dict) and result.get("ok") is False:
                        error_kind = str(result.get("error_code") or "pbi_query_failed")
                    else:
                        status = "ok"
                    return result
                except Exception as exc:
                    logger.warning("builtin_powerbi.call_failed op=%s err=%s", op, type(exc).__name__)
                    error_kind = "pbi_query_failed"
                    return {"ok": False, "error_kind": error_kind, "error_code": error_kind}
                finally:
                    digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
                    try:
                        await store.log_tool_call(
                            team_id=team_id,
                            user_id=ctx.user_id,
                            source_id=tool_id,
                            tool_name=tool_ref,
                            args_digest=digest,
                            status=status,
                            error_kind=error_kind,
                            duration_ms=int((time.perf_counter() - started) * 1000),
                        )
                    except Exception as exc:
                        logger.warning("builtin_powerbi.audit_failed err=%s", type(exc).__name__)

            specs.append(
                ToolSpec(
                    schema=_schema(operation, ref, label),
                    handler=execute,
                    allowed_for_roles=frozenset({"analyst"}),
                    risk_level="low",
                    source="dataset",
                    annotations=ToolAnnotations(open_world=False, destructive=False, read_only=True),
                    team_tool_binding=TeamToolExecutionBinding(team_id=team_id, source_id=tool.id, operation_id=operation, advertised_name=ref),
                )
            )
    return specs
