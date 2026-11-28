"""Approve n8n workflows as derived team tools (``FEATURE_N8N_WORKFLOW_TOOLS``).

Owners and administrators list the MCP-enabled, published workflows of an n8n team source,
open one to see its published trigger and a draft input schema, and approve or remove it.
Approving re-reads the workflow from n8n and stores the published version it saw; the
derived tool runs only that version (``bowhead_core.n8n_workflows``, tech design D1 and D3).
Listing and opening store nothing. Every request to n8n goes through the source's own
credentials and the shared per-host request budget.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urlsplit

from bowhead_core.audit_actions import AuditAction
from bowhead_core.n8n_workflow_run import default_pacer, tool_payload
from bowhead_core.n8n_workflows import (
    MAX_APPROVED_PER_SOURCE,
    MAX_DESCRIPTION_CHARS,
    WEBHOOK_TRIGGER,
    N8nWorkflowApproval,
    NotApprovable,
    TooManyApprovals,
    approvals_from_records,
    build_approval,
    draft_input_schema,
    is_n8n_source,
    load_source_records,
    n8n_tool_ref,
    n8n_workflow_tools_enabled,
    published_nodes,
    runnable_trigger,
    save_n8n_approval,
    valid_workflow_id,
)
from bowhead_core.scrub import scrub
from bowhead_core.user_store import TeamToolSource, User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.routes.auth import _require_team_role_dep, _store

logger = logging.getLogger(__name__)

router = APIRouter()
_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")
_SEARCH_LIMIT = 200
_MESSAGE_CHARS = 300


def _error(status: int, code: str, **details: Any) -> HTTPException:
    error: dict[str, Any] = {"code": code}
    if details:
        error["details"] = details
    return HTTPException(status, detail={"error": error})


async def _n8n_source(store: UserStore, user: User, source_id: str) -> TeamToolSource:
    if not n8n_workflow_tools_enabled():
        raise _error(409, "n8n_workflows_unavailable")
    src = await store.get_team_tool_source(user.team_id, source_id)
    if src is None:
        raise HTTPException(404, "source not found")
    if not is_n8n_source(src.kind, src.base_url, src.tools_schema):
        raise _error(422, "n8n_workflows_not_n8n")
    return src


def _client(request: Request, src: TeamToolSource) -> Any:
    from bowhead_core.openapi_tool_source import RemoteMcpToolClient

    if src.auth_type == "oauth":
        from bowhead_core.mcp_oauth import team_oauth_headers

        store = getattr(request.app.state, "user_store", None)

        async def header_source(rejected: str | None) -> dict[str, str]:
            return await team_oauth_headers(store, src.team_id, src.id, src.base_url, rejected_token=rejected)

        return RemoteMcpToolClient(src.base_url, {}, header_source=header_source)

    async def static_headers() -> dict[str, str]:
        registry = getattr(request.app.state, "tool_auth_registry", None)
        return await registry.get_headers(src.team_id, src.id) if registry is not None else {}

    return _StaticClient(src.base_url, static_headers)


class _StaticClient:
    """A static-credential source: headers are read once per request."""

    def __init__(self, base_url: str, headers: Callable[[], Awaitable[dict[str, str]]]) -> None:
        self.base_url = base_url
        self._headers = headers

    @contextlib.asynccontextmanager
    async def session(self):  # type: ignore[no-untyped-def]
        from bowhead_core.openapi_tool_source import RemoteMcpToolClient

        async with RemoteMcpToolClient(self.base_url, await self._headers()).session() as session:
            yield session


async def _with_n8n[T](request: Request, src: TeamToolSource, calls: int, use: Callable[[Any], Awaitable[T]]) -> T:
    """Run ``use(session)`` against the source's n8n server, mapping failures to coded errors."""
    from bowhead_core.team_tool_registry_view import describe_remote_failure

    host = urlsplit(src.base_url).netloc.lower()
    if not await default_pacer().acquire(host, calls + 2, 5.0):
        raise _error(429, "n8n_rate_limited")
    try:
        async with _client(request, src).session() as session:
            return await use(session)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure becomes a coded, actionable error
        nested = _find_http_exception(exc)
        if nested is not None:
            raise nested from None
        failure = describe_remote_failure(exc, src.id)
        kind = failure.get("error_kind")
        logger.warning("n8n_workflows.request_failed team=%s source=%s error_kind=%s", src.team_id, src.id, kind)
        if kind == "upstream_auth":
            raise _error(409, "n8n_sign_in_ended") from None
        if kind == "rate_limited":
            retry_after = failure.get("retry_after_seconds")
            raise _error(429, "n8n_rate_limited", **({"retry_after_seconds": retry_after} if retry_after is not None else {})) from None
        raise _error(502, "n8n_unreachable", error_kind=str(kind or "protocol")) from None


def _find_http_exception(exc: BaseException, depth: int = 0) -> HTTPException | None:
    """An ``HTTPException`` raised inside the session comes back wrapped by the SDK's task groups."""
    if depth > 6:
        return None
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_http_exception(sub, depth + 1)
            if found is not None:
                return found
    return None


def _refused(body: dict[str, Any]) -> HTTPException:
    message = scrub(str(body.get("error") or "n8n refused the request"))[:_MESSAGE_CHARS]
    return _error(502, "n8n_refused", message=message)


async def _details(session: Any, workflow_id: str) -> dict[str, Any]:
    is_error, body = tool_payload(await session.call_tool("get_workflow_details", {"workflowId": workflow_id, "detailLevel": "full"}))
    workflow = body.get("workflow")
    if is_error or not isinstance(workflow, dict):
        raise _refused(body)
    return workflow


def _trigger_kind(node_type: str) -> str:
    return "webhook" if node_type == WEBHOOK_TRIGGER else "schedule"


def _approval_view(source_id: str, approval: N8nWorkflowApproval | None) -> dict[str, Any] | None:
    if approval is None:
        return None
    return {
        "tool_ref": n8n_tool_ref(source_id, approval.workflow_id),
        "name": approval.name,
        "category": approval.category,
        "description": approval.description,
        "input_schema": approval.input_schema,
        "trigger": {"node": approval.trigger_node, "kind": _trigger_kind(approval.trigger_type)},
        "approved_version_id": approval.approved_version_id,
        "approved_at": approval.approved_at,
    }


async def _approvals(store: UserStore, src: TeamToolSource) -> dict[str, N8nWorkflowApproval]:
    return approvals_from_records(await load_source_records(store.db_path, src.team_id, src.id))


@router.get("/{source_id}/n8n-workflows")
async def list_n8n_workflows(
    source_id: str,
    request: Request,
    response: Response,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """MCP-enabled, published workflows and the stored approvals. Other workflows are only counted."""
    response.headers["Cache-Control"] = "private, no-store"
    src = await _n8n_source(store, user, source_id)

    async def search(session: Any) -> dict[str, Any]:
        is_error, body = tool_payload(await session.call_tool("search_workflows", {"limit": _SEARCH_LIMIT}))
        if is_error or not isinstance(body.get("data"), list):
            raise _refused(body)
        return body

    body = await _with_n8n(request, src, 1, search)
    approvals = await _approvals(store, src)
    items: list[dict[str, Any]] = []
    hidden = 0
    listed: set[str] = set()
    for workflow in body["data"]:
        if not isinstance(workflow, dict) or not valid_workflow_id(workflow.get("id")):
            continue
        if workflow.get("availableInMCP") is not True or workflow.get("active") is not True:
            hidden += 1
            continue
        workflow_id = str(workflow["id"])
        listed.add(workflow_id)
        items.append(
            {
                "id": workflow_id,
                "name": str(workflow.get("name") or workflow_id),
                "description": str(workflow.get("description") or "")[:MAX_DESCRIPTION_CHARS],
                "updated_at": workflow.get("updatedAt"),
                "listed": True,
                "approval": _approval_view(src.id, approvals.get(workflow_id)),
            }
        )
    for workflow_id, approval in sorted(approvals.items()):
        if workflow_id not in listed:
            # Approved, but n8n no longer lists it as MCP-enabled and published: runs will fail until fixed.
            items.append(
                {
                    "id": workflow_id,
                    "name": approval.name,
                    "description": "",
                    "updated_at": None,
                    "listed": False,
                    "approval": _approval_view(src.id, approval),
                }
            )
    count = body.get("count")
    return {
        "items": items,
        "hidden_count": hidden,
        "more_than_listed": isinstance(count, int) and count > len(body["data"]),
        "approval_limit": MAX_APPROVED_PER_SOURCE,
    }


@router.get("/{source_id}/n8n-workflows/{workflow_id}")
async def get_n8n_workflow(
    source_id: str,
    workflow_id: str,
    request: Request,
    response: Response,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """One workflow as n8n reports it now: published trigger, whether it can run, and a draft input schema."""
    response.headers["Cache-Control"] = "private, no-store"
    if not valid_workflow_id(workflow_id):
        raise _error(422, "n8n_workflow_invalid_id")
    src = await _n8n_source(store, user, source_id)
    workflow = await _with_n8n(request, src, 1, lambda session: _details(session, workflow_id))
    trigger = runnable_trigger(workflow_id, workflow)
    approval = (await _approvals(store, src)).get(workflow_id)
    takes_body = not isinstance(trigger, NotApprovable) and trigger[1] == WEBHOOK_TRIGGER
    active_version = workflow.get("activeVersionId")
    return {
        "workflow": {
            "id": workflow_id,
            "name": str(workflow.get("name") or workflow_id),
            "description": str(workflow.get("description") or "")[:MAX_DESCRIPTION_CHARS],
            "active_version_id": active_version if isinstance(active_version, str) else None,
        },
        "trigger": None if isinstance(trigger, NotApprovable) else {"node": trigger[0], "kind": _trigger_kind(trigger[1])},
        "problem": {"code": trigger.code, "message": trigger.message} if isinstance(trigger, NotApprovable) else None,
        "draft_input_schema": draft_input_schema(published_nodes(workflow), takes_body=takes_body),
        "approval": _approval_view(src.id, approval),
        "version_changed": approval is not None and isinstance(active_version, str) and approval.approved_version_id != active_version,
    }


class N8nApprovalBody(BaseModel):
    category: Literal["read", "external_write", "destructive"]
    description: str = Field(..., min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    input_schema: dict[str, Any]


async def _audit(store: UserStore, user: User, source_id: str, workflow_id: str, category: str | None) -> None:
    with contextlib.suppress(Exception):
        await store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=AuditAction.TEAM_TOOL_CATEGORY_CHANGED,
            target_kind="team_tool_source",
            target_id=source_id,
            target_team_id=user.team_id,
            scope="team",
            payload={"changes": [{"tool": f"n8n_wf_{workflow_id}", "category": category}], "n8n_workflow": workflow_id},
        )


@router.put(
    "/{source_id}/n8n-workflows/{workflow_id}",
    dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("mcp"))],
)
async def approve_n8n_workflow(
    source_id: str,
    workflow_id: str,
    body: N8nApprovalBody,
    request: Request,
    response: Response,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Approve (or re-approve) one workflow at its current published version. Applies from the next Turn."""
    response.headers["Cache-Control"] = "private, no-store"
    if not valid_workflow_id(workflow_id):
        raise _error(422, "n8n_workflow_invalid_id")
    src = await _n8n_source(store, user, source_id)
    workflow = await _with_n8n(request, src, 1, lambda session: _details(session, workflow_id))
    approval = build_approval(
        workflow_id=workflow_id,
        workflow=workflow,
        description=body.description,
        input_schema=body.input_schema,
        category=body.category,
        actor_user_id=user.id,
    )
    if isinstance(approval, NotApprovable):
        raise _error(422, "n8n_workflow_not_approvable", reason_code=approval.code, reason=approval.message, detail=approval.detail)
    try:
        stored = await save_n8n_approval(
            store.db_path, team_id=user.team_id, source_id=src.id, workflow_id=workflow_id, approval=approval, actor_user_id=user.id
        )
    except TooManyApprovals:
        raise _error(422, "n8n_workflow_limit", limit=MAX_APPROVED_PER_SOURCE) from None
    if stored is None:
        raise HTTPException(404, "source not found")
    await _audit(store, user, src.id, workflow_id, approval.category)
    return {"approval": _approval_view(src.id, approval)}


@router.delete(
    "/{source_id}/n8n-workflows/{workflow_id}",
    dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("mcp"))],
)
async def remove_n8n_workflow(
    source_id: str,
    workflow_id: str,
    response: Response,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Remove an approval. Its tool disappears from the next Turn; nothing is sent to n8n."""
    response.headers["Cache-Control"] = "private, no-store"
    if not valid_workflow_id(workflow_id):
        raise _error(422, "n8n_workflow_invalid_id")
    src = await _n8n_source(store, user, source_id)
    stored = await save_n8n_approval(
        store.db_path, team_id=user.team_id, source_id=src.id, workflow_id=workflow_id, approval=None, actor_user_id=user.id
    )
    if stored is None:
        raise HTTPException(404, "source not found")
    await _audit(store, user, src.id, workflow_id, None)
    return {"removed": workflow_id}
