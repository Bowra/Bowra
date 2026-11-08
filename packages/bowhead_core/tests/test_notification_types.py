"""The notification type registry and the single emit path built on it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.notification_service import NotificationDraft, NotificationService
from bowhead_core.notification_store import ACTION_REQUIRED_TYPES, NotificationStore
from bowhead_core.notification_team import NotificationSubject
from bowhead_core.notification_types import REGISTRY, NotificationTypeError

from bowhead_core import agent_release_outbox, agent_version_notifications, notification_types


def _run(coro):
    return asyncio.run(coro)


class _Users:
    def __init__(self) -> None:
        self.departed: set[str] = set()

    async def get_user_team_id(self, user_id):
        return "team_legacy"

    async def get_workspace_team_id(self, workspace_id):
        return None

    async def list_team_users_by_roles(self, team_id, roles, *, include_creator=True):
        return ["u_admin", "u_owner"]

    async def filter_active_team_members(self, team_id, user_ids):
        return {uid for uid in user_ids if uid not in self.departed}


def _svc(tmp_path, users: _Users | None = None):
    store = NotificationStore(str(tmp_path / "n.db"))
    _run(store.initialize())
    return NotificationService(store=store, user_store=users or _Users()), store


def _draft(**overrides) -> NotificationDraft:
    values = {
        "type": "content_shared",
        "subject": NotificationSubject("report", "rep_1", team_id="team_1"),
        "recipients": [("u_bob", None)],
        "title": "Shared",
        "body": None,
        "dedupe_key": "share:report:rep_1:grt_1",
    }
    values.update(overrides)
    return NotificationDraft(**values)


def test_action_types_are_exactly_the_badge_set_plus_skill_submissions():
    """Deriving the badge set from the registry must not change what lit the badge before, beyond
    the deliberate addition of Skill submissions (S1)."""
    assert {
        "approval.required",
        "access_requested",
        "access_requested_escalated",
        "access_requests_digest",
        "scheduled.failed",
        "scheduled.paused",
        "routine.needs_input",
        "memory.candidate",
        "space_assignment_requested",
        "agent.release.approval_required",
        "agent.eval.regression_failed",
        "skill_approval_submitted",
    } == ACTION_REQUIRED_TYPES
    assert all(REGISTRY[t].resolution for t in ACTION_REQUIRED_TYPES)


def test_every_outbox_projection_is_registered_with_an_allowed_priority():
    """The outboxes write through their own transactional path; their types still come from the registry."""
    for projection in agent_version_notifications._PROJECTIONS.values():
        notification_types.validate(projection.type, projection.priority)
    release_types = {
        "agent.release.approval_required",
        "agent.release.rejected",
        "agent.release.withdrawn",
        "agent.release.stale",
        "agent.release.updated",
        "agent.release.high_risk",
        "agent.release.audience_contracted",
    }
    for type_ in release_types:
        for risk in ("high", "low"):
            delivery = SimpleNamespace(
                notification_type=type_,
                candidate_id="cand_1",
                agent_id="sa_1",
                event_payload={"risk_level": risk},
            )
            projection = agent_release_outbox._notification_projection(delivery)  # type: ignore[arg-type]
            notification_types.validate(type_, projection["priority"])
    for priority in ("p1", "p2"):
        notification_types.validate("agent.eval.regression_failed", priority)


def test_validate_rejects_unregistered_types_and_priorities_outside_a_type():
    with pytest.raises(NotificationTypeError) as unregistered:
        notification_types.validate("made.up")
    assert unregistered.value.code == "unregistered"
    with pytest.raises(NotificationTypeError) as wrong:
        notification_types.validate("content_shared", "normal")
    assert wrong.value.code == "invalid_priority"


def test_emit_writes_nothing_for_an_unregistered_type_or_an_off_vocabulary_priority(tmp_path):
    svc, store = _svc(tmp_path)

    assert _run(svc.emit(_draft(type="made.up"))) is None
    assert _run(svc.emit(_draft(priority="normal"))) is None

    assert _run(store.list_for_user(user_id="u_bob"))[0] == []


def test_emit_takes_category_and_repeat_behavior_from_the_registry(tmp_path):
    svc, _store = _svc(tmp_path)

    first = _run(svc.emit(_draft()))
    again = _run(svc.emit(_draft(title="Changed")))

    assert first is not None and first.item.category == "share" and first.item.priority == "p2"
    # content_shared is registered `once`: a repeat is not a new event and changes nothing.
    assert again is not None and again.outcome == "unchanged" and again.item.title == "Shared"


def test_emit_drops_recipients_who_have_left_the_team(tmp_path):
    users = _Users()
    users.departed = {"u_gone"}
    svc, store = _svc(tmp_path, users)

    result = _run(svc.emit(_draft(recipients=[("u_bob", None), ("u_gone", None)])))

    assert result is not None and result.delivered_to == ("u_bob",)
    assert _run(store.list_for_user(user_id="u_gone"))[0] == []


def test_emit_without_a_team_reaches_only_the_owner_in_their_own_namespace(tmp_path):
    svc, store = _svc(tmp_path)
    subject = NotificationSubject("scheduled_job", "sj_1", workspace_id="ws_unknown", owner_user_id="u_owner")

    result = _run(
        svc.emit(
            _draft(
                type="scheduled.paused",
                subject=subject,
                recipients=[("u_owner", "owner"), ("u_admin", "admin")],
                dedupe_key="scheduled.paused:sj_1",
            )
        )
    )

    assert result is not None and result.item.team_id == "team_legacy"
    assert result.delivered_to == ("u_owner",)
    assert _run(store.list_for_user(user_id="u_admin"))[0] == []


def test_a_team_item_without_an_owner_is_not_sent_when_no_team_resolves(tmp_path):
    svc, store = _svc(tmp_path)

    result = _run(svc.emit(_draft(subject=NotificationSubject("approval", "apr_1", workspace_id="ws_unknown"))))

    assert result is None
    assert _run(store.list_for_user(user_id="u_bob"))[0] == []


def test_a_skill_resubmission_supersedes_the_open_review_item(tmp_path):
    svc, store = _svc(tmp_path)

    def submit(key: str):
        return _run(
            svc.emit_skill_approval_submitted(
                team_id="team_1",
                skill_id="sk_1",
                skill_label="Pricing",
                submission_key=key,
                actor_user_id="u_author",
                actor_name="Ann",
            )
        )

    first = submit("t1")
    assert submit("t1").id == first.id  # the same submission emitted twice is one item
    second = submit("t2")

    assert second.id != first.id
    assert _run(store.notification_count(user_id="u_admin")).action_required == 1

    decided = _run(
        svc.emit_skill_approval_decided(
            team_id="team_1",
            skill_id="sk_1",
            skill_label="Pricing",
            author_user_id="u_author",
            actor_user_id="u_admin",
            actor_name="Adm",
            decision="approved",
        )
    )
    assert decided is not None and decided.type == "skill_approval_decided"
    assert _run(store.notification_count(user_id="u_admin")).action_required == 0
    assert _run(store.notification_count(user_id="u_owner")).action_required == 0


def test_reconciliation_closes_items_whose_subject_moved_on_and_keeps_the_rest(tmp_path):
    svc, store = _svc(tmp_path)
    for request_id in ("acr_done", "acr_waiting", "acr_broken"):
        _run(
            svc.emit(
                _draft(
                    type="access_requested",
                    subject=NotificationSubject("report", "rep_1", team_id="team_1"),
                    recipients=[("u_admin", "approver")],
                    dedupe_key=f"access.requested:{request_id}",
                    metadata={"requestId": request_id},
                )
            )
        )

    async def check(item):
        request_id = item.metadata["requestId"]
        if request_id == "acr_broken":
            raise RuntimeError("store unavailable")
        return "approved" if request_id == "acr_done" else None

    closed = _run(svc.reconcile_open_actions({"access_requested": check}))

    assert closed == {"access_requested": 1}
    open_keys = {item.dedupe_key for item in _run(store.list_open_items(types=["access_requested"]))}
    assert open_keys == {"access.requested:acr_waiting", "access.requested:acr_broken"}
