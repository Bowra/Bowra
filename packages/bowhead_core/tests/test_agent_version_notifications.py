"""Owner version notifications: proposal writer and the outbox consumer."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore
from bowhead_core.agent_revision_store import AgentRevisionStore
from bowhead_core.agent_version_notifications import (
    MAX_ATTEMPTS,
    AgentVersionNotificationError,
    AgentVersionNotificationStore,
    retry_delay_seconds,
)
from bowhead_core.notification_store import NotificationStore
from test_agent_change_proposal_store import _working
from test_agent_eval_policy_plan_store import (
    test_complete_comparison_creates_one_immutable_ready_snapshot as _ready_comparison,
)
from test_agent_revision_store import _db, _seed, _Validator

pytestmark = pytest.mark.unit

_GROUPS = [{"id": "listing", "operation_indexes": [0], "depends_on": []}]


async def _proposal(
    db_path,
    *,
    source_type="git",
    source_ref="git:run:1",
    created_by_id: str | None = "aap_ci",
    created_by_kind: str | None = None,
):
    store = AgentChangeProposalStore(str(db_path), revision_store=AgentRevisionStore(str(db_path), validator=_Validator()))
    return await store.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type=source_type,
        source_ref=source_ref,
        operations=[{"op": "replace", "path": "/listing/description", "value": "From CI"}],
        atomic_groups=_GROUPS,
        created_by_kind=created_by_kind or ("automation" if created_by_id else "user"),
        created_by_id=created_by_id,
    )


async def _seeded(tmp_path):
    db_path = _db(tmp_path)
    _seed(db_path)
    await _working(db_path)
    notifications = NotificationStore(str(db_path))
    await notifications.initialize()
    return db_path, notifications


def _outbox(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute(
            "SELECT event_type,status,outcome_code,attempt_count,dedupe_key FROM agent_version_notification_outbox ORDER BY created_at,id"
        ).fetchall()


def _make_due(db_path) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE agent_version_notification_outbox SET available_at=? WHERE status='pending'", (past,))
        db.execute("UPDATE agent_version_notification_outbox SET lease_expires_at=? WHERE status='leased'", (past,))


async def _items(notifications, user_id="user_owner"):
    items, _ = await notifications.list_for_user(user_id=user_id)
    return items


@pytest.mark.asyncio
async def test_only_proposals_the_owner_did_not_write_reach_the_outbox(tmp_path) -> None:
    db_path, _ = await _seeded(tmp_path)
    proposal = await _proposal(db_path)
    replay = await _proposal(db_path)
    assert replay.id == proposal.id
    await _proposal(db_path, source_type="coach", source_ref="coach:1")
    await _proposal(db_path, source_type="feedback", source_ref="feedback:1", created_by_id=None)
    # Regression proposals already carry their own regression notification.
    await _proposal(
        db_path,
        source_type="regression",
        source_ref="regression:1",
        created_by_id="system:agent-regression",
        created_by_kind="system",
    )

    assert _outbox(db_path) == [("proposal.created", "pending", None, 0, f"proposal.created:{proposal.id}")]


@pytest.mark.asyncio
async def test_proposal_is_projected_once_across_a_lease_expiry(tmp_path) -> None:
    db_path, notifications = await _seeded(tmp_path)
    proposal = await _proposal(db_path)
    store = AgentVersionNotificationStore(db_path)

    first = await store.claim_version_notification(worker_id="worker-a")
    assert first is not None and first.notice.event_type == "proposal.created"
    assert await store.claim_version_notification(worker_id="worker-b") is None
    outcome = await store.project_version_notification(first, notification_store=notifications)
    assert (outcome.status, outcome.outcome_code) == ("delivered", "notification_projected")
    # worker-a stalls past its lease; worker-b takes the row over and projects it again.
    _make_due(db_path)
    second = await store.claim_version_notification(worker_id="worker-b")
    assert second is not None and second.notice.attempt_count == 2
    await store.project_version_notification(second, notification_store=notifications)
    await store.settle_version_notification(second, status="delivered", outcome_code=outcome.outcome_code)
    with pytest.raises(AgentVersionNotificationError, match="lease_fenced"):
        await store.settle_version_notification(first, status="delivered", outcome_code="notification_projected")

    items = await _items(notifications)
    assert len(items) == 1
    item = items[0]
    assert item.type == "agent.version.proposal_received"
    assert item.action_url == "/agents/manage/?agent_id=osa_release&section=config"
    assert item.metadata["proposal_id"] == proposal.id
    assert item.metadata["groupCount"] == 1
    assert item.metadata["agentName"]
    assert "From CI" not in f"{item.title} {item.body} {item.metadata}"
    assert _outbox(db_path)[0][1:3] == ("delivered", "notification_projected")
    assert await store.claim_version_notification(worker_id="worker-c") is None


@pytest.mark.asyncio
async def test_a_recipient_who_left_the_team_is_settled_as_failed(tmp_path) -> None:
    db_path, notifications = await _seeded(tmp_path)
    await _proposal(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_members SET status='removed' WHERE user_id='user_owner'")
    store = AgentVersionNotificationStore(db_path)
    claim = await store.claim_version_notification(worker_id="worker-a")
    assert claim is not None
    outcome = await store.project_version_notification(claim, notification_store=notifications)
    assert (outcome.status, outcome.outcome_code) == ("failed", "recipient_inactive")
    await store.settle_version_notification(claim, status=outcome.status, outcome_code=outcome.outcome_code)
    assert _outbox(db_path)[0][1:3] == ("failed", "recipient_inactive")
    assert await _items(notifications) == []


@pytest.mark.asyncio
async def test_failed_projection_backs_off_then_fails_after_the_last_attempt(tmp_path) -> None:
    db_path, _ = await _seeded(tmp_path)
    await _proposal(db_path)
    store = AgentVersionNotificationStore(db_path)
    assert [retry_delay_seconds(n) for n in (1, 2, 3, 8)] == [30, 60, 120, 3600]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claim = await store.claim_version_notification(worker_id="worker-a")
        assert claim is not None and claim.notice.attempt_count == attempt
        before = datetime.now(UTC)
        await store.retry_version_notification(claim, outcome_code="notification_store_down")
        with sqlite3.connect(db_path) as db:
            status, available_at = db.execute("SELECT status,available_at FROM agent_version_notification_outbox").fetchone()
        if attempt < MAX_ATTEMPTS:
            assert status == "pending"
            delay = (datetime.fromisoformat(available_at) - before).total_seconds()
            assert retry_delay_seconds(attempt) - 1 <= delay <= retry_delay_seconds(attempt) + 1
            assert await store.claim_version_notification(worker_id="worker-a") is None
            _make_due(db_path)
    assert _outbox(db_path)[0][1:4] == ("failed", "notification_store_down", MAX_ATTEMPTS)


@pytest.mark.asyncio
async def test_a_proposal_closed_before_delivery_is_settled_without_a_notice(tmp_path) -> None:
    db_path, notifications = await _seeded(tmp_path)
    proposal = await _proposal(db_path)
    store = AgentChangeProposalStore(str(db_path), revision_store=AgentRevisionStore(str(db_path), validator=_Validator()))
    await store.dismiss(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
        expected_proposal_row_version=proposal.row_version,
    )
    consumer = AgentVersionNotificationStore(db_path)
    claim = await consumer.claim_version_notification(worker_id="worker-a")
    assert claim is not None and claim.notice.proposal_open is False
    outcome = await consumer.project_version_notification(claim, notification_store=notifications)
    assert (outcome.status, outcome.outcome_code) == ("delivered", "proposal_closed")
    assert await _items(notifications) == []


@pytest.mark.asyncio
async def test_a_crashed_last_attempt_is_settled_instead_of_reclaimed(tmp_path) -> None:
    db_path, _ = await _seeded(tmp_path)
    await _proposal(db_path)
    store = AgentVersionNotificationStore(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE agent_version_notification_outbox SET attempt_count=?", (MAX_ATTEMPTS - 1,))
    claim = await store.claim_version_notification(worker_id="worker-a")
    assert claim is not None and claim.notice.attempt_count == MAX_ATTEMPTS
    # worker-a dies without settling; its lease expires after the last allowed attempt.
    _make_due(db_path)
    assert await store.claim_version_notification(worker_id="worker-b") is None
    assert _outbox(db_path)[0][1:4] == ("failed", "lease_expired_attempts_exhausted", MAX_ATTEMPTS)
    with pytest.raises(AgentVersionNotificationError, match="lease_fenced"):
        await store.settle_version_notification(claim, status="delivered", outcome_code="notification_projected")


@pytest.mark.asyncio
async def test_results_are_projected_and_owner_preparation_is_suppressed(tmp_path) -> None:
    await _ready_comparison(tmp_path)
    db_path = tmp_path / "core.db"
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as db:
        # The fixture prepared the candidate as the Owner, by hand.
        assert db.execute("SELECT prepared_actor_kind FROM agent_eval_candidate_control").fetchone() == ("user",)
        db.execute(
            """INSERT INTO agent_version_notification_outbox(id,team_id,agent_id,recipient_user_id,event_type,candidate_id,
                   dedupe_key,safe_payload_json,status,available_at,created_at)
               VALUES('aevn_prepared','team_p','osa_policy','user_owner','candidate.prepared','arc_policy',
                      'agent-version:arc_policy:prepared:user_owner','{"schema_version":1}','pending',?,'0000')""",
            (now,),
        )
    notifications = NotificationStore(str(db_path))
    await notifications.initialize()
    store = AgentVersionNotificationStore(db_path)
    outcomes = {}
    while (claim := await store.claim_version_notification(worker_id="worker-a")) is not None:
        outcome = await store.project_version_notification(claim, notification_store=notifications)
        await store.settle_version_notification(claim, status=outcome.status, outcome_code=outcome.outcome_code)
        outcomes[claim.notice.event_type] = outcome.outcome_code

    assert outcomes == {
        "candidate.prepared": "self_action",
        "comparison.completed": "not_projected",
        "candidate.ready": "notification_projected",
    }
    items = await _items(notifications)
    assert [(item.type, item.metadata["readiness"], item.action_url) for item in items] == [
        ("agent.version.result_ready", "ready", "/agents/manage/?agent_id=osa_policy&section=release"),
    ]
