"""Deliver the Agent Owner version outbox into the notification inbox.

Writers append ``agent_version_notification_outbox`` rows in the same
transaction as the proposal, candidate or comparison they describe. This
consumer leases the oldest due row, projects it into ``NotificationStore`` and
settles it. Every row ends ``delivered`` or ``failed`` with an outcome code, so
the table drains even for events that are not shown to the Owner.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.notification_store import NotificationStore

MAX_ATTEMPTS = 8
_BASE_BACKOFF_SECONDS = 30
_MAX_BACKOFF_SECONDS = 3600

# Readiness events already reach the Owner; review events reach reviewers and
# Owners through the release outbox; release.regression has its own regression
# notification. These rows are settled without a projection.
_NOT_PROJECTED = frozenset({"comparison.completed", "review.submitted", "review.rejected", "release.regression"})


@dataclass(frozen=True, slots=True)
class _Projection:
    type: str
    priority: Literal["p1", "p2", "p3"]
    task: Literal["config", "release"]
    title: str
    body: str


_PROJECTIONS: dict[str, _Projection] = {
    "proposal.created": _Projection(
        "agent.version.proposal_received",
        "p2",
        "config",
        "A change suggestion is waiting",
        "Review the suggested change on Configure.",
    ),
    "candidate.prepared": _Projection(
        "agent.version.candidate_prepared",
        "p3",
        "release",
        "A version was prepared",
        "Evaluations can now run for the prepared version.",
    ),
    "candidate.ready": _Projection(
        "agent.version.result_ready",
        "p2",
        "release",
        "A version is ready to release",
        "The release check passed. Open Release to continue.",
    ),
    "candidate.blocked": _Projection(
        "agent.version.result_blocked",
        "p1",
        "release",
        "A version is blocked",
        "The release check found problems. Open Release to see what failed.",
    ),
    "candidate.unknown": _Projection(
        "agent.version.result_unknown",
        "p2",
        "release",
        "A release check could not finish",
        "The result is unknown. Open Release to see why and run it again.",
    ),
    "candidate.stale": _Projection(
        "agent.version.candidate_stale",
        "p2",
        "release",
        "A prepared version is out of date",
        "The Agent changed after this version was prepared. Prepare it again on Release.",
    ),
}


class AgentVersionNotificationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentVersionNotice:
    outbox_id: str
    team_id: str
    agent_id: str
    agent_name: str
    recipient_user_id: str
    event_type: str
    candidate_id: str | None
    payload: dict[str, Any]
    dedupe_key: str
    prepared_actor_kind: str | None
    recipient_is_owner: bool
    recipient_is_member: bool
    reason_count: int | None
    proposal_open: bool
    lease_generation: int
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ClaimedAgentVersionNotice:
    notice: AgentVersionNotice
    lease_token: str


@dataclass(frozen=True, slots=True)
class AgentVersionNoticeOutcome:
    status: Literal["delivered", "failed"]
    outcome_code: str


def retry_delay_seconds(attempt_count: int) -> int:
    """30 s for the first failed attempt, doubling per attempt, capped at one hour."""

    return min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * 2 ** max(0, attempt_count - 1))


class AgentVersionNotificationStore:
    """Lease, project and settle Owner version notifications."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def claim_version_notification(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> ClaimedAgentVersionNotice | None:
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                # A lease that expired after its last allowed attempt (the worker crashed
                # mid-delivery) is settled, not claimed again: the cap holds across crashes.
                await db.execute(
                    """
                    UPDATE agent_version_notification_outbox
                    SET status='failed',outcome_code='lease_expired_attempts_exhausted',settled_at=?,
                        lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL
                    WHERE status='leased' AND julianday(lease_expires_at)<=julianday(?) AND attempt_count>=?
                    """,
                    (now.isoformat(), now.isoformat(), MAX_ATTEMPTS),
                )
                async with db.execute(
                    """
                    SELECT outbox.*,
                           COALESCE(NULLIF(TRIM(agent.display_name),''),agent.name,'') AS agent_name,
                           COALESCE(agent.owner_user_id,agent.created_by)=outbox.recipient_user_id AS recipient_is_owner,
                           EXISTS(
                             SELECT 1 FROM users recipient
                             JOIN team_members member
                               ON member.user_id=recipient.id AND member.team_id=outbox.team_id
                              AND member.status='active'
                             WHERE recipient.id=outbox.recipient_user_id AND recipient.status='active'
                           ) AS recipient_is_member,
                           control.prepared_actor_kind,
                           snapshot.summary_json AS snapshot_summary_json,
                           proposal.status AS proposal_status
                    FROM agent_version_notification_outbox outbox
                    JOIN team_subagent agent ON agent.team_id=outbox.team_id AND agent.id=outbox.agent_id
                    LEFT JOIN agent_eval_candidate_control control ON control.candidate_id=outbox.candidate_id
                    LEFT JOIN agent_eval_snapshot snapshot ON snapshot.id=outbox.snapshot_id
                    LEFT JOIN agent_change_proposal proposal
                      ON outbox.event_type='proposal.created'
                     AND proposal.id=json_extract(outbox.safe_payload_json,'$.proposal_id')
                     AND proposal.team_id=outbox.team_id AND proposal.agent_id=outbox.agent_id
                    WHERE (outbox.status='pending' AND julianday(outbox.available_at)<=julianday(?))
                       OR (outbox.status='leased' AND julianday(outbox.lease_expires_at)<=julianday(?))
                    ORDER BY outbox.created_at,outbox.id LIMIT 1
                    """,
                    (now.isoformat(), now.isoformat()),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                generation = int(row["lease_generation"]) + 1
                updated = await db.execute(
                    """
                    UPDATE agent_version_notification_outbox
                    SET status='leased',lease_generation=?,lease_owner=?,
                        lease_token_hash=?,lease_expires_at=?,
                        attempt_count=attempt_count+1,outcome_code=NULL
                    WHERE id=? AND (
                      (status='pending' AND julianday(available_at)<=julianday(?))
                      OR (status='leased' AND julianday(lease_expires_at)<=julianday(?))
                    )
                    """,
                    (
                        generation,
                        worker_id,
                        _hash_token(token),
                        (now + timedelta(seconds=lease_seconds)).isoformat(),
                        row["id"],
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                if updated.rowcount != 1:
                    await db.commit()
                    return None
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return ClaimedAgentVersionNotice(
            notice=AgentVersionNotice(
                outbox_id=str(row["id"]),
                team_id=str(row["team_id"]),
                agent_id=str(row["agent_id"]),
                agent_name=str(row["agent_name"] or ""),
                recipient_user_id=str(row["recipient_user_id"]),
                event_type=str(row["event_type"]),
                candidate_id=str(row["candidate_id"]) if row["candidate_id"] is not None else None,
                payload=_object(row["safe_payload_json"]),
                dedupe_key=str(row["dedupe_key"]),
                prepared_actor_kind=(str(row["prepared_actor_kind"]) if row["prepared_actor_kind"] is not None else None),
                recipient_is_owner=bool(row["recipient_is_owner"]),
                recipient_is_member=bool(row["recipient_is_member"]),
                reason_count=_reason_count(row["snapshot_summary_json"]),
                proposal_open=row["proposal_status"] == "open",
                lease_generation=generation,
                attempt_count=int(row["attempt_count"]) + 1,
            ),
            lease_token=token,
        )

    async def project_version_notification(
        self,
        claim: ClaimedAgentVersionNotice,
        *,
        notification_store: NotificationStore,
    ) -> AgentVersionNoticeOutcome:
        """Project one leased row, or say why it is settled without a notification."""

        notice = claim.notice
        projection = _PROJECTIONS.get(notice.event_type)
        if projection is None:
            if notice.event_type in _NOT_PROJECTED:
                return AgentVersionNoticeOutcome("delivered", "not_projected")
            raise AgentVersionNotificationError("agent_version_notification_event_unsupported")
        if notice.event_type == "candidate.prepared" and notice.prepared_actor_kind == "user":
            # The Owner prepared it by hand; only CI or Coach preparation is news to them.
            return AgentVersionNoticeOutcome("delivered", "self_action")
        if notice.event_type == "proposal.created" and not notice.proposal_open:
            # Applied, dismissed or superseded before delivery: nothing is waiting any more.
            return AgentVersionNoticeOutcome("delivered", "proposal_closed")
        if not notice.recipient_is_member or not notice.recipient_is_owner:
            return AgentVersionNoticeOutcome("failed", "recipient_inactive")
        readiness = notice.payload.get("readiness")
        readiness = str(readiness) if readiness in {"ready", "ready_with_risks", "blocked", "unknown"} else None
        proposal_id = notice.payload.get("proposal_id") if notice.event_type == "proposal.created" else None
        url = f"/agents/manage/?agent_id={quote(notice.agent_id, safe='')}&section={projection.task}"
        title = projection.title
        if readiness == "ready_with_risks":
            title = "A version is ready to release with risks"
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "agent_id": notice.agent_id,
            "event_type": notice.event_type,
            "outbox_id": notice.outbox_id,
            # Frontend notification-copy.ts names the Agent in the localized title.
            **({"agentName": notice.agent_name} if notice.agent_name else {}),
            **({"candidate_id": notice.candidate_id} if notice.candidate_id else {}),
            **({"proposal_id": str(proposal_id)} if isinstance(proposal_id, str) else {}),
            **({"readiness": readiness} if readiness else {}),
        }
        group_count = notice.payload.get("group_count")
        if notice.event_type == "proposal.created" and isinstance(group_count, int):
            metadata["groupCount"] = group_count
        if notice.event_type in {"candidate.blocked", "candidate.unknown"} and notice.reason_count is not None:
            metadata["reasonCount"] = notice.reason_count
        subject_type, subject_id = (
            ("agent_change_proposal", str(proposal_id))
            if isinstance(proposal_id, str)
            else ("agent_release_candidate", notice.candidate_id)
            if notice.candidate_id
            else ("team_subagent", notice.agent_id)
        )
        await notification_store.upsert_item_with_recipients(
            team_id=notice.team_id,
            # Updates, not the action tab: an action item stays open until something resolves it,
            # and nothing resolves these once the Owner acts on the workbench. Tab counters carry the signal.
            category="updates",
            type=projection.type,
            priority=projection.priority,
            title=title,
            body=projection.body,
            subject_type=subject_type,
            subject_id=subject_id,
            actor_user_id=None,
            action_url=url,
            actions=[{"id": "open", "label": "Open", "url": url}],
            metadata=metadata,
            dedupe_key=notice.dedupe_key,
            recipients=[(notice.recipient_user_id, "owner")],
            reopen_resolved=False,
        )
        return AgentVersionNoticeOutcome("delivered", "notification_projected")

    async def settle_version_notification(
        self,
        claim: ClaimedAgentVersionNotice,
        *,
        status: Literal["delivered", "failed"],
        outcome_code: str,
    ) -> None:
        await self._release_lease(
            claim,
            assignments="status=?,outcome_code=?,settled_at=?",
            values=(status, outcome_code[:160], datetime.now(UTC).isoformat()),
        )

    async def retry_version_notification(
        self,
        claim: ClaimedAgentVersionNotice,
        *,
        outcome_code: str,
    ) -> None:
        """Back off a failed projection; the last allowed attempt settles as failed."""

        if claim.notice.attempt_count >= MAX_ATTEMPTS:
            await self.settle_version_notification(claim, status="failed", outcome_code=outcome_code)
            return
        available_at = datetime.now(UTC) + timedelta(seconds=retry_delay_seconds(claim.notice.attempt_count))
        await self._release_lease(
            claim,
            assignments="status='pending',lease_generation=0,outcome_code=?,available_at=?",
            values=(outcome_code[:160], available_at.isoformat()),
        )

    async def _release_lease(
        self,
        claim: ClaimedAgentVersionNotice,
        *,
        assignments: str,
        values: tuple[Any, ...],
    ) -> None:
        notice = claim.notice
        async with open_shared_ctx(self.db_path) as db:
            result = await db.execute(
                f"""
                UPDATE agent_version_notification_outbox
                SET {assignments},lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL
                WHERE id=? AND status='leased' AND lease_generation=? AND lease_token_hash=?
                """,  # noqa: S608 - assignments are fixed strings from this module
                (*values, notice.outbox_id, notice.lease_generation, _hash_token(claim.lease_token)),
            )
            await db.commit()
        if result.rowcount != 1:
            raise AgentVersionNotificationError("agent_version_notification_lease_fenced")


def _hash_token(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def _object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _reason_count(summary_json: Any) -> int | None:
    if summary_json is None:
        return None
    reasons = _object(summary_json).get("reason_codes")
    return len(reasons) if isinstance(reasons, list) else None


__all__ = [
    "MAX_ATTEMPTS",
    "AgentVersionNotice",
    "AgentVersionNoticeOutcome",
    "AgentVersionNotificationError",
    "AgentVersionNotificationStore",
    "ClaimedAgentVersionNotice",
    "retry_delay_seconds",
]
