"""Gateway helpers shared by protected Skill mutation routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bowhead_core.resource_impact import (
    ResourceChangeIdempotencyConflict,
    ResourceImpactKeyUnavailable,
    ResourceImpactTokenInvalid,
    ResourceImpactTokenStale,
)
from fastapi import HTTPException, Request


def impact_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ResourceImpactKeyUnavailable):
        return HTTPException(503, detail={"error": {"code": exc.code}})
    if isinstance(exc, (ResourceChangeIdempotencyConflict, ResourceImpactTokenStale)):
        return HTTPException(409, detail={"error": {"code": exc.code}})
    if isinstance(exc, ResourceImpactTokenInvalid):
        return HTTPException(422, detail={"error": {"code": exc.code}})
    return HTTPException(503, detail={"error": {"code": "resource_impact_unavailable"}})


def require_impact_headers(request: Request) -> tuple[str, str]:
    token = request.headers.get("X-Bowhead-Impact-Token", "").strip()
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not token or not 16 <= len(idempotency_key) <= 128:
        raise HTTPException(412, detail={"error": {"code": "resource_impact_required"}})
    return token, idempotency_key


def usage_dependency_impact(view: Any, schedule_ids: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "resource_usage": view.to_dict(),
        "affected_schedule_ids": sorted(schedule_ids),
        **extra,
    }


def delete_has_live_dependencies(view: Any) -> bool:
    summary = view.summary
    return bool(summary.current_agent_count or summary.running_task_count or summary.draft_agent_count)


async def affected_skill_schedule_ids(
    db: Any,
    *,
    team_id: str,
    resource_ref: str,
) -> list[str]:
    async with db.execute(
        """
        SELECT DISTINCT binding.job_id
        FROM resource_dependency_edge edge
        JOIN scheduled_job_agent_binding binding
          ON binding.agent_id=edge.agent_id AND binding.revision_ref=edge.subject_ref
        JOIN scheduled_jobs job ON job.id=binding.job_id
        JOIN workspaces workspace ON workspace.id=job.workspace_id
        WHERE edge.team_id=? AND workspace.team_id=?
          AND edge.resource_kind='skill' AND edge.resource_ref=?
          AND binding.state='ready' AND job.status='active'
        ORDER BY binding.job_id
        """,
        (team_id, team_id, resource_ref),
    ) as cursor:
        return [str(row[0]) for row in await cursor.fetchall()]


async def pause_affected_schedules(db: Any, schedule_ids: list[str]) -> None:
    if not schedule_ids:
        return
    placeholders = ",".join("?" for _ in schedule_ids)
    await db.execute(
        f"UPDATE scheduled_job_agent_binding SET state='needs_reconfirm',row_version=row_version+1 "
        f"WHERE job_id IN ({placeholders}) AND state='ready'",  # noqa: S608 -- placeholders only
        schedule_ids,
    )
    await db.execute(
        f"UPDATE scheduled_jobs SET status='paused',pause_reason='resource_dependency_changed',updated_at=? "
        f"WHERE id IN ({placeholders}) AND status='active'",  # noqa: S608 -- placeholders only
        (datetime.now(UTC).isoformat(), *schedule_ids),
    )


__all__ = [
    "affected_skill_schedule_ids",
    "delete_has_live_dependencies",
    "impact_http_error",
    "pause_affected_schedules",
    "require_impact_headers",
    "usage_dependency_impact",
]
