"""Notifications follow the Team that owns their subject, never the legacy ``users.team_id``.

Every case uses a user whose legacy default Team is ``team_a`` while the subject's workspace
belongs to ``team_b``: the multi-Team situation in which the two answers differ.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore


def _run(coro):
    return asyncio.run(coro)


class _MultiTeamUsers:
    def __init__(self, workspace_teams: dict[str, str] | None = None) -> None:
        self.workspace_teams = {"ws_b": "team_b"} if workspace_teams is None else workspace_teams
        self.admins = {"team_a": ["u_admin_a"], "team_b": ["u_admin_b"]}
        self.departed: dict[str, set[str]] = {}

    async def get_user_team_id(self, user_id):
        return "team_a"

    async def get_workspace_team_id(self, workspace_id):
        return self.workspace_teams.get(workspace_id)

    async def list_team_users_by_roles(self, team_id, roles, *, include_creator=True):
        return list(self.admins.get(team_id, []))

    async def filter_active_team_members(self, team_id, user_ids):
        departed = self.departed.get(team_id, set())
        return {uid for uid in user_ids if uid not in departed}


def _svc(tmp_path, users: _MultiTeamUsers | None = None):
    store = NotificationStore(str(tmp_path / "n.db"))
    _run(store.initialize())
    return NotificationService(store=store, user_store=users or _MultiTeamUsers()), store


def _approval(status: str = "pending", workspace_id: str = "ws_b"):
    return SimpleNamespace(
        id="apr_1",
        user_id="u_req",
        workspace_id=workspace_id,
        status=status,
        tool_name="crm_update",
        risk_level="high",
        turn_id="t_1",
        session_id="ses_1",
        decided_by="u_admin_b",
        execution_error=None,
    )


def _job(workspace_id: str = "ws_b"):
    return SimpleNamespace(id="sj_1", user_id="u_owner", workspace_id=workspace_id, name="Daily sales", failure_count=3)


def _action_count(store: NotificationStore, user_id: str) -> int:
    return _run(store.notification_count(user_id=user_id)).action_required


def _legacy_item(store: NotificationStore, *, type_: str, dedupe_key: str, subject_type: str, subject_id: str, recipient: str):
    """An open item written under the legacy Team, as releases before this fix did."""

    _run(
        store.write(
            team_id="team_a",
            category="test",
            type=type_,
            priority="p1",
            title="legacy",
            body=None,
            subject_type=subject_type,
            subject_id=subject_id,
            actor_user_id=None,
            action_url=None,
            actions=None,
            metadata={},
            dedupe_key=dedupe_key,
            recipients=[(recipient, None)],
        )
    )


def test_approval_required_notifies_the_workspace_team_administrators(tmp_path):
    svc, store = _svc(tmp_path)

    item = _run(svc.emit_approval_required(_approval()))

    assert item is not None and item.team_id == "team_b"
    assert _action_count(store, "u_admin_b") == 1
    assert _action_count(store, "u_admin_a") == 0


def test_approval_required_is_not_sent_without_a_workspace_team(tmp_path):
    svc, store = _svc(tmp_path, _MultiTeamUsers(workspace_teams={}))

    assert _run(svc.emit_approval_required(_approval())) is None
    assert _action_count(store, "u_admin_a") == 0


def test_requester_update_without_a_workspace_team_reaches_only_the_requester(tmp_path):
    svc, store = _svc(tmp_path, _MultiTeamUsers(workspace_teams={}))

    item = _run(svc.emit_approval_decided(_approval(status="approved")))

    assert item is not None and item.team_id == "team_a"
    requester_items, _ = _run(store.list_for_user(user_id="u_req"))
    admin_items, _ = _run(store.list_for_user(user_id="u_admin_a"))
    assert [row.type for row in requester_items] == ["approval.approved"]
    assert admin_items == []


def test_resolving_an_approval_closes_an_item_written_under_the_legacy_team(tmp_path):
    svc, store = _svc(tmp_path)
    _legacy_item(
        store,
        type_="approval.required",
        dedupe_key="approval.required:apr_1",
        subject_type="approval",
        subject_id="apr_1",
        recipient="u_admin_a",
    )
    assert _action_count(store, "u_admin_a") == 1

    resolved = _run(svc.resolve_approval_required(_approval(status="approved"), reason="approved"))

    assert resolved is not None and resolved.team_id == "team_a"
    assert _action_count(store, "u_admin_a") == 0


def test_paused_job_notifies_its_owner_and_only_its_own_team_administrators(tmp_path):
    svc, store = _svc(tmp_path)

    item = _run(svc.emit_scheduled_paused(_job()))

    assert item is not None and item.team_id == "team_b"
    assert _action_count(store, "u_owner") == 1
    assert _action_count(store, "u_admin_b") == 1
    assert _action_count(store, "u_admin_a") == 0


def test_paused_job_without_a_workspace_team_tells_only_the_owner(tmp_path):
    svc, store = _svc(tmp_path, _MultiTeamUsers(workspace_teams={}))

    item = _run(svc.emit_scheduled_paused(_job()))

    assert item is not None
    assert _action_count(store, "u_owner") == 1
    assert _action_count(store, "u_admin_a") == 0


def test_model_unconfigured_pause_without_a_workspace_team_tells_only_the_owner(tmp_path):
    svc, store = _svc(tmp_path, _MultiTeamUsers(workspace_teams={}))

    item = _run(svc.emit_scheduled_paused(_job(), reason="team_model_unconfigured"))

    assert item is not None and item.subject_type == "scheduled_job"
    assert _action_count(store, "u_owner") == 1
    assert _action_count(store, "u_admin_a") == 0


def test_model_unconfigured_pause_tells_the_job_team_administrators(tmp_path):
    svc, store = _svc(tmp_path)

    _run(svc.emit_scheduled_paused(_job(), reason="team_model_unconfigured"))

    admin_items, _ = _run(store.list_for_user(user_id="u_admin_b"))
    assert [(row.team_id, row.subject_id) for row in admin_items] == [("team_b", "team_b")]
    assert _action_count(store, "u_admin_a") == 0


def test_owner_items_use_the_job_workspace_team(tmp_path):
    svc, _store = _svc(tmp_path)

    failed = _run(svc.emit_scheduled_failed(_job(), None))
    waiting = _run(svc.emit_routine_needs_input(_job(), run_id="run_1", session_id="ses_1"))

    assert failed is not None and failed.team_id == "team_b"
    assert waiting is not None and waiting.team_id == "team_b"


def test_answering_closes_a_waiting_item_written_under_the_legacy_team(tmp_path):
    svc, store = _svc(tmp_path)
    _legacy_item(
        store,
        type_="routine.needs_input",
        dedupe_key="routine.needs_input:sj_1:run_1",
        subject_type="scheduled_job",
        subject_id="sj_1",
        recipient="u_owner",
    )

    resolved = _run(svc.resolve_routine_needs_input(_job(), run_id="run_1"))

    assert resolved is not None
    assert _action_count(store, "u_owner") == 0


def test_recovery_and_deletion_close_items_under_both_teams(tmp_path):
    svc, store = _svc(tmp_path)
    _legacy_item(
        store,
        type_="scheduled.failed",
        dedupe_key="scheduled.failed:sj_1",
        subject_type="scheduled_job",
        subject_id="sj_1",
        recipient="u_owner",
    )
    _run(svc.emit_scheduled_failed(_job(), None))
    assert _action_count(store, "u_owner") == 2

    _run(svc.emit_scheduled_recovered(_job(), "run_2"))
    assert _action_count(store, "u_owner") == 0

    _legacy_item(
        store,
        type_="scheduled.paused",
        dedupe_key="scheduled.paused:sj_1:legacy",
        subject_type="scheduled_job",
        subject_id="sj_1",
        recipient="u_owner",
    )
    _run(svc.emit_routine_needs_input(_job(), run_id="run_3", session_id=None))
    assert _action_count(store, "u_owner") == 2

    closed = _run(svc.resolve_scheduled_job_notifications(_job()))

    # team_b: the recovered notice and the waiting item; team_a: the legacy pause.
    assert closed == 3
    assert _action_count(store, "u_owner") == 0
