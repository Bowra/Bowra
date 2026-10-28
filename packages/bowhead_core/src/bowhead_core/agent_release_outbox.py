"""Durable Agent release notification outbox projection.

Release transactions freeze one row per recipient.  This worker projects each
row into the existing Notification Center with at-least-once delivery: a crash
after projection but before acknowledgement is harmless because the frozen
outbox dedupe key is also the Notification Center dedupe key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.audit_actions import AuditAction
from bowhead_core.notification_store import NotificationStore
from bowhead_core.user_store import UserStore

_DEFAULT_LEASE = timedelta(seconds=30)
_DEFAULT_MAX_ATTEMPTS = 8
_MAX_BACKOFF_SECONDS = 60 * 60


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class AgentReleaseOutboxForbiddenError(PermissionError):
    code = "agent_release_outbox_replay_forbidden"


class AgentReleaseOutboxNotFoundError(LookupError):
    code = "agent_release_event_not_found"


@dataclass(frozen=True, slots=True)
class AgentReleaseOutboxDelivery:
    id: str
    release_event_id: str
    recipient_user_id: str
    notification_type: str
    dedupe_key: str
    attempt_count: int
    team_id: str
    agent_id: str
    candidate_id: str | None
    release_id: str | None
    event_type: str
    actor_user_id: str
    event_payload: dict[str, Any]
    # Display-name snapshot taken at projection so the notification keeps naming
    # the Agent even after a rename or deletion.
    agent_name: str = ""


@dataclass(frozen=True, slots=True)
class AgentReleaseOutboxDrainResult:
    claimed: int
    delivered: int
    retried: int
    failed: int


class AgentReleaseNotificationOutbox:
    def __init__(
        self,
        db_path: str,
        *,
        notification_store: NotificationStore,
        clock: Clock | None = None,
        lease_duration: timedelta = _DEFAULT_LEASE,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if lease_duration.total_seconds() <= 0:
            raise ValueError("Agent release outbox lease must be positive")
        if max_attempts < 1:
            raise ValueError("Agent release outbox max attempts must be positive")
        self.db_path = str(db_path)
        self._notifications = notification_store
        self._clock = clock or SystemClock()
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts

    async def drain_once(
        self,
        *,
        worker_id: str,
        limit: int = 50,
    ) -> AgentReleaseOutboxDrainResult:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("Agent release outbox worker id is invalid")
        limit = max(1, min(limit, 500))
        claimed = delivered = retried = failed = 0
        for _ in range(limit):
            delivery = await self._claim_one(worker_id=worker_id)
            if delivery is None:
                break
            claimed += 1
            try:
                await self._project(delivery)
            except Exception as exc:
                terminal = await self._record_failure(
                    delivery,
                    worker_id=worker_id,
                    error_code=_safe_error_code(exc),
                )
                failed += int(terminal)
                retried += int(not terminal)
            else:
                if await self._mark_delivered(delivery, worker_id=worker_id):
                    delivered += 1
        return AgentReleaseOutboxDrainResult(
            claimed=claimed,
            delivered=delivered,
            retried=retried,
            failed=failed,
        )

    async def replay_failed_event(
        self,
        *,
        team_id: str,
        event_id: str,
        actor_user_id: str,
    ) -> int:
        """Reset failed rows after rechecking the current reviewer role."""

        timestamp = self._now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT subagent_id FROM team_subagent_release_event WHERE id=? AND team_id=?",
                    (event_id, team_id),
                ) as cursor:
                    event = await cursor.fetchone()
                if event is None:
                    raise AgentReleaseOutboxNotFoundError(
                        "Agent release event not found",
                    )
                if not await _qualified_reviewer_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                ):
                    raise AgentReleaseOutboxForbiddenError(
                        "Current team owner or Team Admin required",
                    )
                updated = await db.execute(
                    """
                    UPDATE team_subagent_notification_outbox
                    SET state='pending',attempt_count=0,next_attempt_at=?,
                        lease_owner=NULL,lease_expires_at=NULL,delivered_at=NULL,
                        last_error_code=NULL
                    WHERE release_event_id=? AND state='failed'
                    """,
                    (timestamp, event_id),
                )
                replayed = max(0, int(updated.rowcount or 0))
                if replayed:
                    await UserStore(self.db_path).write_admin_audit_log(
                        actor_user_id=actor_user_id,
                        team_id=team_id,
                        action=AuditAction.AGENT_RELEASE_NOTIFICATION_REPLAYED,
                        target_kind="team_subagent",
                        target_id=str(event["subagent_id"]),
                        target_team_id=team_id,
                        scope="team",
                        payload={
                            "event_id": event_id,
                            "recipient_count": replayed,
                        },
                        conn=db,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return replayed

    async def _claim_one(
        self,
        *,
        worker_id: str,
    ) -> AgentReleaseOutboxDelivery | None:
        now = self._clock.now().astimezone(UTC)
        timestamp = now.isoformat()
        lease_expires_at = (now + self._lease_duration).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT o.*,e.team_id,e.subagent_id,e.candidate_id,e.release_id,
                           e.event_type,e.actor_user_id,e.payload_json AS event_payload_json,
                           COALESCE(NULLIF(TRIM(s.display_name),''),s.name,'') AS agent_name
                    FROM team_subagent_notification_outbox o
                    JOIN team_subagent_release_event e ON e.id=o.release_event_id
                    LEFT JOIN team_subagent s ON s.team_id=e.team_id AND s.id=e.subagent_id
                    WHERE (
                      (o.state='pending' AND o.next_attempt_at<=?)
                      OR (o.state='sending' AND o.lease_expires_at<=?)
                    )
                    ORDER BY o.next_attempt_at ASC,o.created_at ASC,o.id ASC
                    LIMIT 1
                    """,
                    (timestamp, timestamp),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                updated = await db.execute(
                    """
                    UPDATE team_subagent_notification_outbox
                    SET state='sending',attempt_count=attempt_count+1,
                        lease_owner=?,lease_expires_at=?
                    WHERE id=? AND (
                      (state='pending' AND next_attempt_at<=?)
                      OR (state='sending' AND lease_expires_at<=?)
                    )
                    """,
                    (
                        worker_id,
                        lease_expires_at,
                        row["id"],
                        timestamp,
                        timestamp,
                    ),
                )
                if updated.rowcount != 1:
                    await db.commit()
                    return None
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentReleaseOutboxDelivery(
            id=str(row["id"]),
            release_event_id=str(row["release_event_id"]),
            recipient_user_id=str(row["recipient_user_id"]),
            notification_type=str(row["notification_type"]),
            dedupe_key=str(row["dedupe_key"]),
            attempt_count=int(row["attempt_count"]) + 1,
            team_id=str(row["team_id"]),
            agent_id=str(row["subagent_id"]),
            candidate_id=(str(row["candidate_id"]) if row["candidate_id"] is not None else None),
            release_id=(str(row["release_id"]) if row["release_id"] is not None else None),
            event_type=str(row["event_type"]),
            actor_user_id=str(row["actor_user_id"]),
            event_payload=_safe_json_object(row["event_payload_json"]),
            agent_name=str(row["agent_name"] or ""),
        )

    async def _project(self, delivery: AgentReleaseOutboxDelivery) -> None:
        projection = _notification_projection(delivery)
        if delivery.candidate_id and delivery.event_type in {
            "candidate.rejected",
            "candidate.withdrawn",
            "candidate.stale",
            "release.activated",
        }:
            await self._notifications.resolve_open_by_subject(
                team_id=delivery.team_id,
                subject_type="agent_release_candidate",
                subject_id=delivery.candidate_id,
                reason=delivery.event_type,
            )
        await self._notifications.upsert_item_with_recipients(
            team_id=delivery.team_id,
            category=projection["category"],
            type=delivery.notification_type,
            priority=projection["priority"],
            title=projection["title"],
            body=projection["body"],
            subject_type=projection["subject_type"],
            subject_id=projection["subject_id"],
            actor_user_id=delivery.actor_user_id,
            action_url=projection["action_url"],
            actions=projection["actions"],
            metadata={
                "schema_version": 1,
                "release_event_id": delivery.release_event_id,
                "agent_id": delivery.agent_id,
                "candidate_id": delivery.candidate_id,
                "release_id": delivery.release_id,
                "event_type": delivery.event_type,
                "risk_level": _safe_risk_level(delivery.event_payload),
                # Frontend notification-copy.ts names the Agent in the localized title.
                **({"agentName": delivery.agent_name} if delivery.agent_name else {}),
            },
            dedupe_key=delivery.dedupe_key,
            recipients=[(delivery.recipient_user_id, "recipient")],
            reopen_resolved=False,
        )

    async def _mark_delivered(
        self,
        delivery: AgentReleaseOutboxDelivery,
        *,
        worker_id: str,
    ) -> bool:
        async with open_shared_ctx(self.db_path) as db:
            updated = await db.execute(
                """
                UPDATE team_subagent_notification_outbox
                SET state='delivered',delivered_at=?,lease_owner=NULL,
                    lease_expires_at=NULL,last_error_code=NULL
                WHERE id=? AND state='sending' AND lease_owner=?
                """,
                (self._now(), delivery.id, worker_id),
            )
            await db.commit()
        return updated.rowcount == 1

    async def _record_failure(
        self,
        delivery: AgentReleaseOutboxDelivery,
        *,
        worker_id: str,
        error_code: str,
    ) -> bool:
        terminal = delivery.attempt_count >= self._max_attempts
        next_attempt_at = (self._clock.now().astimezone(UTC) + timedelta(seconds=_backoff_seconds(delivery.id, delivery.attempt_count))).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            updated = await db.execute(
                """
                UPDATE team_subagent_notification_outbox
                SET state=?,next_attempt_at=?,lease_owner=NULL,lease_expires_at=NULL,
                    last_error_code=?
                WHERE id=? AND state='sending' AND lease_owner=?
                """,
                (
                    "failed" if terminal else "pending",
                    next_attempt_at,
                    error_code,
                    delivery.id,
                    worker_id,
                ),
            )
            await db.commit()
        return terminal and updated.rowcount == 1

    def _now(self) -> str:
        return self._clock.now().astimezone(UTC).isoformat()


async def _qualified_reviewer_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_user_id: str,
) -> bool:
    async with db.execute(
        """
        SELECT 1
        FROM users u
        JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
        JOIN teams t ON t.id=?
        WHERE u.id=? AND u.status='active'
          AND (u.id=t.created_by_user_id OR tm.role='team_admin')
        """,
        (team_id, team_id, actor_user_id),
    ) as cursor:
        return await cursor.fetchone() is not None


def _notification_projection(delivery: AgentReleaseOutboxDelivery) -> dict[str, Any]:
    if delivery.notification_type == "agent.release.approval_required":
        if not delivery.candidate_id:
            raise ValueError("Agent approval notification has no candidate")
        url = f"/agents/manage/?release_candidate={delivery.candidate_id}"
        return {
            "category": "action",
            "priority": "p1" if _safe_risk_level(delivery.event_payload) == "high" else "p2",
            "title": "Agent release needs review",
            "body": "Review the submitted version, access scope, and risk changes.",
            "subject_type": "agent_release_candidate",
            "subject_id": delivery.candidate_id,
            "action_url": url,
            "actions": [{"id": "review", "label": "Review", "url": url}],
        }
    titles = {
        "agent.release.rejected": "Agent release was rejected",
        "agent.release.withdrawn": "Agent release was withdrawn",
        "agent.release.stale": "Agent release needs to be resubmitted",
        "agent.release.updated": "An Agent you hired was updated",
        "agent.release.high_risk": "Important Agent capability update",
        "agent.release.audience_contracted": "Agent access was reduced",
    }
    title = titles.get(delivery.notification_type)
    if title is None:
        raise ValueError("Unsupported Agent release notification type")
    url = f"/agents/detail/?id={delivery.agent_id}"
    return {
        "category": "updates",
        "priority": ("p1" if delivery.notification_type in {"agent.release.high_risk", "agent.release.audience_contracted"} else "p2"),
        "title": title,
        "body": "Open the Agent to review the current version and availability.",
        "subject_type": "team_subagent",
        "subject_id": delivery.agent_id,
        "action_url": url,
        "actions": [],
    }


def _safe_json_object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Agent release event payload is malformed") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Agent release event payload is malformed")
    return decoded


def _safe_risk_level(payload: dict[str, Any]) -> str | None:
    value = payload.get("risk_level")
    return str(value) if value in {"low", "medium", "high", "unknown"} else None


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return "agent_release_notification_payload_invalid"
    return "agent_release_notification_projection_failed"


def _backoff_seconds(outbox_id: str, attempt: int) -> int:
    base = min(_MAX_BACKOFF_SECONDS, 2 ** min(max(attempt, 1), 12))
    digest = hashlib.sha256(f"{outbox_id}:{attempt}".encode()).digest()
    jitter = int.from_bytes(digest[:2], "big") % max(1, base // 4)
    return min(_MAX_BACKOFF_SECONDS, base + jitter)


__all__ = [
    "AgentReleaseNotificationOutbox",
    "AgentReleaseOutboxDelivery",
    "AgentReleaseOutboxDrainResult",
    "AgentReleaseOutboxForbiddenError",
    "AgentReleaseOutboxNotFoundError",
]
