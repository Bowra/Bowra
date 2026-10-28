"""Homepage read model.

The snapshot is deliberately deterministic. It projects recipient-scoped
notifications and native workload records into a small homepage shape without
creating a second inbox or asking an LLM to infer status.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bowhead_gateway.product_identity import product_name
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.notifications import normalize_notification_action_url

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter()
logger = logging.getLogger(__name__)


class HomeScopeDTO(BaseModel):
    workspaceId: str
    workspaceName: str


class HomeSourceIssueDTO(BaseModel):
    source: str
    code: str


class HomeAttentionItemDTO(BaseModel):
    id: str
    source: Literal["notification"] = "notification"
    nativeType: str
    category: str = ""
    priority: str
    title: str
    body: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actionUrl: str
    subjectType: str | None = None
    subjectId: str | None = None
    createdAt: str


class HomeProgressDTO(BaseModel):
    completed: int
    total: int
    failed: int = 0
    etaSeconds: int | None = None


class HomeActiveItemDTO(BaseModel):
    id: str
    engine: Literal["chat", "routine", "matrix"]
    nativeStatus: str
    title: str
    subtitle: str | None = None
    progress: HomeProgressDTO | None = None
    actionUrl: str
    sessionId: str | None = None
    startedAt: str | None = None
    updatedAt: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HomeOutputItemDTO(BaseModel):
    id: str
    kind: Literal["report", "deliverable"]
    title: str
    format: str | None = None
    actionUrl: str
    sessionId: str | None = None
    createdAt: str
    updatedAt: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class HomeCountsDTO(BaseModel):
    attention: int = 0
    active: int = 0
    outputs: int = 0


class HomeSnapshotDTO(BaseModel):
    version: Literal["home.snapshot.v1"] = "home.snapshot.v1"
    generatedAt: str
    scope: HomeScopeDTO
    attentionScope: Literal["user"] = "user"
    attention: list[HomeAttentionItemDTO] = Field(default_factory=list)
    active: list[HomeActiveItemDTO] = Field(default_factory=list)
    outputs: list[HomeOutputItemDTO] = Field(default_factory=list)
    counts: HomeCountsDTO
    partial: bool = False
    issues: list[HomeSourceIssueDTO] = Field(default_factory=list)


@dataclass(frozen=True)
class _WorkspaceScope:
    workspace_id: str
    workspace_name: str
    query_workspace_ids: tuple[str, ...]


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _user_store(request: Request) -> UserStore:
    store: UserStore | None = getattr(request.app.state, "user_store", None)
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "user store not configured")
    return store


async def _resolve_scope(user: User, store: UserStore, workspace_id: str) -> _WorkspaceScope:
    requested = workspace_id.strip()
    if not requested:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "workspace_id required")
    for row in await store.list_workspaces_for_user(user.id):
        canonical = str(row.get("workspace_id") or "")
        datarc_id = str(row.get("datarc_workspace_id") or "")
        if requested != canonical:
            continue
        query_ids = tuple(dict.fromkeys(value for value in (canonical, datarc_id) if value))
        return _WorkspaceScope(
            workspace_id=canonical,
            workspace_name=str(row.get("name") or canonical),
            query_workspace_ids=query_ids,
        )
    raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")


async def _collect_attention(
    request: Request,
    user: User,
) -> tuple[list[HomeAttentionItemDTO], int]:
    if getattr(request.app.state, "notification_center_enabled", True) is False:
        raise RuntimeError("notification center disabled")
    store = getattr(request.app.state, "notification_store", None)
    if store is None:
        raise RuntimeError("notification store unavailable")
    rows, _cursor = await store.list_for_user(
        user_id=user.id,
        tab="action",
        limit=10,
    )
    count = await store.notification_count(user_id=user.id)
    items = [
        HomeAttentionItemDTO(
            id=row.id,
            nativeType=row.type,
            category=row.category,
            priority=row.priority,
            title=row.title,
            body=row.body,
            metadata=row.metadata or {},
            actionUrl=normalize_notification_action_url(row.action_url) or "",
            subjectType=row.subject_type,
            subjectId=row.subject_id,
            createdAt=row.created_at,
        )
        for row in rows
        if row.state == "open" and row.action_url
    ][:3]
    return items, count.action_required


async def _sessions_for_scope(request: Request, user: User, scope: _WorkspaceScope) -> list[Any]:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise RuntimeError("session store unavailable")
    by_id: dict[str, Any] = {}
    for workspace_id in scope.query_workspace_ids:
        rows = await store.list_sessions(
            user_id=user.id,
            workspace_id=workspace_id,
            limit=100,
        )
        for row in rows:
            if getattr(row, "origin", "normal") == "normal":
                by_id.setdefault(row.id, row)
    return sorted(by_id.values(), key=lambda item: item.last_active_at or "", reverse=True)


async def _collect_active_turns(
    request: Request,
    sessions: list[Any],
) -> list[HomeActiveItemDTO]:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise RuntimeError("session store unavailable")
    items: list[HomeActiveItemDTO] = []
    for session in sessions:
        # ``list_sessions`` already selected ``sessions.active_turn_id`` (the
        # authoritative running signal). Re-reading it per session opened one
        # SQLite connection each -- up to 100 per snapshot, ~20-45ms CPU apiece
        # on the production schema -- only to learn that almost all are idle.
        turn_id = getattr(session, "active_turn_id", None)
        if not turn_id:
            continue
        turn = await store.get_turn(turn_id)
        if turn is None or turn.status != "running":
            continue
        title = (session.title or turn.user_text or "").strip() or f"{product_name()} work"
        items.append(
            HomeActiveItemDTO(
                id=f"chat:{turn.id}",
                engine="chat",
                nativeStatus=turn.status,
                title=title[:120],
                actionUrl=f"/?session={quote(session.id, safe='')}",
                sessionId=session.id,
                startedAt=turn.started_at,
                updatedAt=session.last_active_at,
                metadata={"turnId": turn.id, "agentType": turn.agent_type or "lead"},
            )
        )
    return items


async def _collect_active_routines(
    request: Request,
    user: User,
    scope: _WorkspaceScope,
) -> list[HomeActiveItemDTO]:
    store = getattr(request.app.state, "scheduled_store", None)
    if store is None:
        raise RuntimeError("scheduled store unavailable")
    items: list[HomeActiveItemDTO] = []
    jobs = [job for job in await store.list_for_user(user.id) if job.workspace_id in scope.query_workspace_ids][:100]
    for job in jobs:
        runs = await store.list_runs(job.id, limit=1)
        if not runs or runs[0].status != "running":
            continue
        run = runs[0]
        items.append(
            HomeActiveItemDTO(
                id=f"routine:{run.id}",
                engine="routine",
                nativeStatus=run.status,
                title=job.name,
                actionUrl="/team/settings/scheduled/",
                startedAt=run.started_at,
                updatedAt=run.started_at,
                metadata={"jobId": job.id, "runId": run.id},
            )
        )
    return items


async def _collect_active_matrices(
    request: Request,
    scope: _WorkspaceScope,
) -> list[HomeActiveItemDTO]:
    store = getattr(request.app.state, "kb_matrix_store", None)
    if store is None:
        raise RuntimeError("matrix store unavailable")
    items: list[HomeActiveItemDTO] = []
    seen: set[str] = set()
    for workspace_id in scope.query_workspace_ids:
        for matrix in (await store.list_matrices(workspace_id))[:8]:
            if matrix.matrix_id in seen:
                continue
            seen.add(matrix.matrix_id)
            runs = await store.list_runs_for_snapshot(matrix.matrix_id)
            for run in runs:
                if run.status not in {"running", "paused", "cancelling"}:
                    continue
                remaining = max(0, run.cells_total - run.cells_done - run.cells_failed)
                items.append(
                    HomeActiveItemDTO(
                        id=f"matrix:{run.run_id}",
                        engine="matrix",
                        nativeStatus=run.status,
                        title=matrix.name,
                        progress=HomeProgressDTO(
                            completed=run.cells_done,
                            total=run.cells_total,
                            failed=run.cells_failed,
                        ),
                        actionUrl=f"/knowledge?tab=matrix&view={quote(matrix.matrix_id, safe='')}",
                        startedAt=run.started_at,
                        updatedAt=run.heartbeat_at,
                        metadata={
                            "matrixId": matrix.matrix_id,
                            "runId": run.run_id,
                            "remaining": remaining,
                        },
                    )
                )
    return items


def _report_title(row: dict[str, Any]) -> str:
    """The stored title, else the first section heading, else empty.

    An empty title lets the client build its localized "<date> Report" fallback from
    ``createdAt``, the same one the report list uses.
    """
    for key in ("title", "first_section_title"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


async def _collect_reports(
    request: Request,
    user: User,
    scope: _WorkspaceScope,
) -> list[HomeOutputItemDTO]:
    canvas_store = getattr(request.app.state, "canvas_store", None)
    if canvas_store is None:
        raise RuntimeError("canvas store unavailable")

    canvases: dict[str, dict[str, Any]] = {}
    for workspace_id in scope.query_workspace_ids:
        for row in await canvas_store.list_for_user(
            user_id=user.id,
            workspace_id=workspace_id,
            sort="updated_desc",
            limit=8,
            include_archived=False,
            # Empty-canvas cleanup: move criterion into store, before LIMIT. Previously fetched 8 then filtered
            # sections_count>0; empty shells consumed slots, underreporting recent output
            # (116's heaviest space returned 5 of 8).
            min_sections=1,
        ):
            canvases.setdefault(str(row.get("id")), row)

    return [
        HomeOutputItemDTO(
            id=f"report:{row['id']}",
            kind="report",
            title=_report_title(row),
            actionUrl=f"/?session={quote(str(row['session_id']), safe='')}&report=1",
            sessionId=str(row["session_id"]),
            createdAt=str(row.get("created_at") or ""),
            updatedAt=str(row.get("updated_at") or row.get("created_at") or ""),
            metadata={
                "canvasId": str(row["id"]),
                "sectionsCount": int(row.get("sections_count") or 0),
            },
        )
        for row in canvases.values()
    ]


async def _collect_deliverables(
    request: Request,
    user: User,
    scope: _WorkspaceScope,
    report_canvas_ids: set[str],
) -> list[HomeOutputItemDTO]:
    artifact_store = getattr(request.app.state, "artifact_store", None)
    session_store = getattr(request.app.state, "session_store", None)
    if artifact_store is None or session_store is None:
        raise RuntimeError("artifact or session store unavailable")

    artifacts, _total, _size = await artifact_store.list_for_user(
        user_id=user.id,
        sources=None,
        sort="created_desc",
        limit=24,
        offset=0,
        include_deleted=False,
    )
    outputs: list[HomeOutputItemDTO] = []
    for artifact in artifacts:
        if artifact.kind not in {"deliverable", "canvas_export"}:
            continue
        if artifact.kind == "canvas_export" and artifact.canvas_id in report_canvas_ids:
            continue
        turn = await session_store.get_turn(artifact.created_by_turn) if artifact.created_by_turn else None
        session = await session_store.get_session(turn.session_id) if turn else None
        if session is None or session.deleted_at is not None or session.user_id != user.id:
            continue
        if session.workspace_id not in scope.query_workspace_ids:
            continue
        suffix = PurePath(artifact.filename).suffix.lower().lstrip(".") or None
        outputs.append(
            HomeOutputItemDTO(
                id=f"artifact:{artifact.id}",
                kind="deliverable",
                title=artifact.filename,
                format=suffix,
                actionUrl=f"/?session={quote(session.id, safe='')}",
                sessionId=session.id,
                createdAt=artifact.created_at,
                updatedAt=artifact.created_at,
                metadata={"artifactId": artifact.id},
            )
        )

    return outputs


def _record_issue(issues: list[HomeSourceIssueDTO], source: str, exc: Exception) -> None:
    logger.warning("home_snapshot_source_failed source=%s error=%s", source, type(exc).__name__)
    issues.append(HomeSourceIssueDTO(source=source, code="source_unavailable"))


def _record_source_metric(source: str, outcome: str, started_at: float) -> None:
    try:
        from bowhead_core import metrics

        labels = {"source": source, "outcome": outcome}
        metrics.inc(
            "bowhead_home_source_total",
            labels=labels,
            help="Homepage snapshot source outcomes.",
        )
        metrics.observe(
            "bowhead_home_source_latency_ms",
            value=(monotonic() - started_at) * 1000,
            labels={"source": source},
            buckets=(10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0),
            help="Homepage snapshot source latency in milliseconds.",
        )
    except Exception:  # noqa: S110
        pass


def _record_snapshot_metric(outcome: str, started_at: float) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_home_snapshot_total",
            labels={"outcome": outcome},
            help="Homepage snapshot request outcomes.",
        )
        metrics.observe(
            "bowhead_home_snapshot_latency_ms",
            value=(monotonic() - started_at) * 1000,
            buckets=(25.0, 50.0, 100.0, 250.0, 500.0, 800.0, 1500.0, 3000.0),
            help="Homepage snapshot request latency in milliseconds.",
        )
    except Exception:  # noqa: S110
        pass


@router.get("/snapshot", response_model=HomeSnapshotDTO)
async def get_home_snapshot(
    workspace_id: str,
    request: Request,
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store),
) -> HomeSnapshotDTO:
    request_started = monotonic()
    if os.environ.get("BOWHEAD_HOME_SNAPSHOT", "1") != "1":
        _record_snapshot_metric("disabled", request_started)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "code": "home_snapshot_disabled",
                "message": "home snapshot disabled",
            },
        )
    try:
        scope = await _resolve_scope(user, user_store, workspace_id)
    except HTTPException:
        _record_snapshot_metric("scope_rejected", request_started)
        raise
    issues: list[HomeSourceIssueDTO] = []

    attention: list[HomeAttentionItemDTO] = []
    attention_count = 0
    source_started = monotonic()
    try:
        attention, attention_count = await _collect_attention(request, user)
        _record_source_metric("notifications", "success", source_started)
    except Exception as exc:
        _record_issue(issues, "notifications", exc)
        _record_source_metric("notifications", "error", source_started)

    active: list[HomeActiveItemDTO] = []
    source_started = monotonic()
    try:
        sessions = await _sessions_for_scope(request, user, scope)
        if sessions:
            active.extend(await _collect_active_turns(request, sessions))
        _record_source_metric("turns", "success", source_started)
    except Exception as exc:
        _record_issue(issues, "turns", exc)
        _record_source_metric("turns", "error", source_started)

    source_started = monotonic()
    try:
        active.extend(await _collect_active_routines(request, user, scope))
        _record_source_metric("routines", "success", source_started)
    except Exception as exc:
        _record_issue(issues, "routines", exc)
        _record_source_metric("routines", "error", source_started)
    source_started = monotonic()
    try:
        active.extend(await _collect_active_matrices(request, scope))
        _record_source_metric("matrix", "success", source_started)
    except Exception as exc:
        _record_issue(issues, "matrix", exc)
        _record_source_metric("matrix", "error", source_started)
    active = sorted(active, key=lambda item: item.updatedAt or item.startedAt or "", reverse=True)[:4]

    outputs: list[HomeOutputItemDTO] = []
    report_canvas_ids: set[str] = set()
    source_started = monotonic()
    try:
        reports = await _collect_reports(request, user, scope)
        outputs.extend(reports)
        report_canvas_ids = {str(item.metadata["canvasId"]) for item in reports if item.metadata.get("canvasId")}
        _record_source_metric("reports", "success", source_started)
    except Exception as exc:
        _record_issue(issues, "reports", exc)
        _record_source_metric("reports", "error", source_started)
    source_started = monotonic()
    try:
        outputs.extend(
            await _collect_deliverables(request, user, scope, report_canvas_ids),
        )
        _record_source_metric("artifacts", "success", source_started)
    except Exception as exc:
        _record_issue(issues, "artifacts", exc)
        _record_source_metric("artifacts", "error", source_started)
    outputs = sorted(outputs, key=lambda item: item.updatedAt, reverse=True)[:4]

    result = HomeSnapshotDTO(
        generatedAt=_now(),
        scope=HomeScopeDTO(
            workspaceId=scope.workspace_id,
            workspaceName=scope.workspace_name,
        ),
        attention=attention,
        active=active,
        outputs=outputs,
        counts=HomeCountsDTO(
            attention=attention_count,
            active=len(active),
            outputs=len(outputs),
        ),
        partial=bool(issues),
        issues=issues,
    )
    _record_snapshot_metric("partial" if issues else "success", request_started)
    return result
