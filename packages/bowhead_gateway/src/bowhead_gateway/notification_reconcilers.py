"""Readers of each action notification's subject, for the sweeper's reconciliation pass.

Each check returns the reason to close an open action item whose subject no longer asks for
anything, or None to keep it. Producers already close their items on the normal path; these
checks close the ones a missed call (a crash, an older release, a path that never closed it)
would otherwise leave lit on the badge indefinitely. A store that is not available contributes
no check. Build the checks once per sweep: some read a snapshot when first used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bowhead_core.notification_store import NotificationItem


def build_reconcile_checks(
    *,
    access_request_store: Any | None = None,
    scheduled_store: Any | None = None,
    user_store: Any | None = None,
) -> dict[str, Callable[[NotificationItem], Awaitable[str | None]]]:
    checks: dict[str, Callable[[NotificationItem], Awaitable[str | None]]] = {}

    if access_request_store is not None:

        async def access_request(item: NotificationItem) -> str | None:
            request_id = str(item.metadata.get("requestId") or "")
            if not request_id:
                return None
            req = await access_request_store.get(request_id, team_id=item.team_id)
            if req is None:
                return "request_missing"
            return None if req.status == "pending" else str(req.status)

        checks["access_requested"] = access_request
        checks["access_requested_escalated"] = access_request

    if scheduled_store is not None:

        async def scheduled_paused(item: NotificationItem) -> str | None:
            # The per-Team "model not configured" item is about the Team, not one task.
            if item.subject_type != "scheduled_job" or not item.subject_id:
                return None
            job = await scheduled_store.get(item.subject_id)
            if job is None:
                return "job_deleted"
            return None if job.status == "paused" else "not_paused"

        async def scheduled_failed(item: NotificationItem) -> str | None:
            if item.subject_type != "scheduled_job" or not item.subject_id:
                return None
            return "job_deleted" if await scheduled_store.get(item.subject_id) is None else None

        awaiting: dict[str, set[str]] = {}

        async def routine_needs_input(item: NotificationItem) -> str | None:
            run_id = str(item.metadata.get("runId") or "")
            if not run_id:
                return None
            # Read once per sweep, after the open items were listed: a run waiting when its item
            # was written is still in this set unless it has since stopped waiting.
            if "runs" not in awaiting:
                awaiting["runs"] = {run.id for run in await scheduled_store.list_awaiting_runs()}
            return None if run_id in awaiting["runs"] else "not_awaiting"

        checks["scheduled.paused"] = scheduled_paused
        checks["scheduled.failed"] = scheduled_failed
        checks["routine.needs_input"] = routine_needs_input

    if user_store is not None:

        async def skill_submission(item: NotificationItem) -> str | None:
            if not item.subject_id:
                return None
            sk = await user_store.get_team_skill(item.team_id, item.subject_id)
            if sk is None:
                return "deleted"
            pending = sk.approval == "pending" or sk.candidate_state == "pending"
            return None if pending else "not_pending"

        checks["skill_approval_submitted"] = skill_submission

    return checks
