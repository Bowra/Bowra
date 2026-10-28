from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.approval_store import Approval
from bowhead_core.clarification_store import ClarificationRequest
from bowhead_core.session_store import Session
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.user_store import User
from bowhead_gateway.routes import approvals, clarifications, sessions, turns
from fastapi import HTTPException


class _UserStore:
    def __init__(self) -> None:
        self.status = "active"
        self.removed: set[str] = set()

    async def get_user_team_id(self, user_id: str) -> str | None:
        return "team_a" if user_id in {"user_a", "requester"} else None

    async def get_user(self, user_id: str) -> User | None:
        team_id = await self.get_user_team_id(user_id)
        if team_id is None:
            return None
        return User(
            id=user_id,
            team_id=team_id,
            email=f"{user_id}@example.com",
            status="active",
        )

    async def get_workspace_team_id(self, workspace_id: str) -> str | None:
        return "team_a" if workspace_id == "ws_a" else None

    async def get_member_role(
        self,
        workspace_id: str,
        user_id: str,
    ) -> str | None:
        if workspace_id != "ws_a" or user_id in self.removed:
            return None
        return "admin" if user_id == "user_a" else "member"

    async def get_workspace(self, workspace_id: str):
        if workspace_id != "ws_a":
            return None
        return {
            "id": workspace_id,
            "team_id": "team_a",
            "status": self.status,
        }

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        if user_id == "user_a" and team_id == "team_a":
            return "team_admin"
        return "member" if team_id == "team_a" else None


def _request(user_store: _UserStore):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=user_store,
                access_control_store=None,
                settings=SimpleNamespace(
                    auth_mode="prod",
                    dev_default_user="unused",
                ),
            )
        ),
        headers={},
        cookies={},
    )


def _session() -> Session:
    return Session(
        id="session_a",
        workspace_id="ws_a",
        user_id="user_a",
        title="History",
        started_at="2026-01-01T00:00:00Z",
        last_active_at="2026-01-01T00:00:00Z",
        ended_at=None,
        status="active",
        cost_usd=0,
        tokens_total=0,
        intents=[],
    )


class _SessionStore:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.seen_writes = 0

    async def get_session(self, session_id: str) -> Session | None:
        return self.session if session_id == self.session.id else None

    async def set_session_seen(self, *, user_id: str, session_id: str) -> None:
        self.seen_writes += 1


@pytest.mark.integration
async def test_session_lifecycle_matrix_and_write_has_no_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_store = _UserStore()
    request = _request(user_store)
    store = _SessionStore(_session())
    monkeypatch.setattr(sessions, "_user_id", lambda _request: "user_a")

    assert (
        await sessions._assert_session_access(
            "session_a",
            request,
            store,
            operation=SpaceOperation.READ_HISTORY,
        )
    ).id == "session_a"
    assert (
        await sessions._assert_session_access(
            "session_a",
            request,
            store,
            operation=SpaceOperation.WRITE,
        )
    ).id == "session_a"

    user_store.status = "archived"
    assert (
        await sessions._assert_session_access(
            "session_a",
            request,
            store,
            operation=SpaceOperation.READ_HISTORY,
        )
    ).id == "session_a"

    with pytest.raises(HTTPException) as archived:
        await sessions.mark_session_seen(
            "session_a",
            request,
            store,
        )
    assert archived.value.status_code == 409
    assert archived.value.detail["error"]["code"] == "workspace_archived"
    assert store.seen_writes == 0

    user_store.removed.add("user_a")
    with pytest.raises(HTTPException) as removed:
        await sessions._assert_session_access(
            "session_a",
            request,
            store,
            operation=SpaceOperation.READ_HISTORY,
        )
    assert removed.value.status_code == 403


def _clarification() -> ClarificationRequest:
    return ClarificationRequest(
        request_id="clarification_a",
        turn_id="turn_a",
        user_id="user_a",
        workspace_id="ws_a",
        token="revenue",
        rationale="ambiguous",
        candidates=[],
    )


class _ClarificationStore:
    def __init__(self, clarification: ClarificationRequest) -> None:
        self.clarification = clarification
        self.dismiss_writes = 0

    async def get(self, request_id: str) -> ClarificationRequest | None:
        if request_id == self.clarification.request_id:
            return self.clarification
        return None

    async def mark_dismissed(
        self,
        request_id: str,
    ) -> ClarificationRequest | None:
        self.dismiss_writes += 1
        return self.clarification

    async def get_pending_for_turn(
        self,
        turn_id: str,
    ) -> list[ClarificationRequest]:
        return [self.clarification] if turn_id == self.clarification.turn_id else []


@pytest.mark.integration
async def test_clarification_lifecycle_matrix_and_dismiss_has_no_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_store = _UserStore()
    request = _request(user_store)
    clarification = _clarification()
    store = _ClarificationStore(clarification)
    monkeypatch.setattr(
        clarifications,
        "_current_user_id",
        lambda _request: "user_a",
    )

    await clarifications._require_clarification_lifecycle(
        clarification,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    await clarifications._require_clarification_lifecycle(
        clarification,
        request,
        operation=SpaceOperation.EXECUTE,
    )
    user_store.status = "archived"
    await clarifications._require_clarification_lifecycle(
        clarification,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )

    with pytest.raises(HTTPException) as archived:
        await clarifications.dismiss_clarification(
            "clarification_a",
            request,
            store,
        )
    assert archived.value.status_code == 409
    assert archived.value.detail["error"]["code"] == "workspace_archived"
    assert store.dismiss_writes == 0

    user_store.removed.add("user_a")
    with pytest.raises(HTTPException) as removed:
        await clarifications._require_clarification_lifecycle(
            clarification,
            request,
            operation=SpaceOperation.READ_HISTORY,
        )
    assert removed.value.status_code == 403


@pytest.mark.integration
async def test_clarification_routes_use_inventory_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(_UserStore())
    clarification = _clarification()
    store = _ClarificationStore(clarification)
    seen: list[SpaceOperation] = []

    monkeypatch.setattr(
        clarifications,
        "_current_user_id",
        lambda _request: "user_a",
    )

    async def gate(_clarification, _request, *, operation):
        seen.append(operation)
        raise HTTPException(409, {"error": {"code": "test_stop"}})

    monkeypatch.setattr(
        clarifications,
        "_require_clarification_lifecycle",
        gate,
    )

    calls = (
        lambda: clarifications.get_one("clarification_a", request, store),
        lambda: clarifications.resolve_one(
            "clarification_a",
            clarifications.ResolveRequest(chosen_value="revenue"),
            request,
            SimpleNamespace(),
            store,
            SimpleNamespace(),
        ),
        lambda: clarifications.dismiss_clarification(
            "clarification_a",
            request,
            store,
        ),
        lambda: clarifications.resume_turn(
            "clarification_a",
            request,
            SimpleNamespace(),
            store,
        ),
        lambda: clarifications.cancel_directive_endpoint(
            "clarification_a",
            request,
            store,
        ),
    )
    for call in calls:
        with pytest.raises(HTTPException):
            await call()

    assert seen == [
        SpaceOperation.READ_HISTORY,
        SpaceOperation.EXECUTE,
        SpaceOperation.WRITE,
        SpaceOperation.EXECUTE,
        SpaceOperation.EXECUTE,
    ]

    turn_operations: list[SpaceOperation] = []

    async def require_turn(
        turn_id,
        request,
        user=None,
        *,
        operation=SpaceOperation.READ_HISTORY,
    ):
        turn_operations.append(operation)
        return SimpleNamespace(id="user_a")

    monkeypatch.setattr(turns, "_require_turn_access", require_turn)
    result = await clarifications.list_for_turn("turn_a", request, store)
    assert [item.request_id for item in result.items] == ["clarification_a"]
    assert turn_operations == [SpaceOperation.READ_HISTORY]


def _approval() -> Approval:
    return Approval(
        id="approval_a",
        turn_id="turn_a",
        session_id="session_a",
        workspace_id="ws_a",
        member_uid="member_a",
        user_id="requester",
        tool_name="write_tool",
        arguments={},
        request_key=None,
        arguments_digest=None,
        reason=None,
        risk_level="high",
        status="pending",
        decided_by=None,
        decided_at=None,
        execution_ok=None,
        execution_result=None,
        execution_error=None,
        executed_at=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


class _ApprovalStore:
    def __init__(self, approval: Approval) -> None:
        self.approval = approval
        self.decision_writes = 0

    async def get(self, approval_id: str) -> Approval | None:
        return self.approval if approval_id == self.approval.id else None

    async def set_decision_once(self, **_kwargs):
        self.decision_writes += 1
        return self.approval, True


@pytest.mark.integration
async def test_approval_lifecycle_matrix_and_decision_has_no_side_effect() -> None:
    user_store = _UserStore()
    request = _request(user_store)
    approval = _approval()
    store = _ApprovalStore(approval)
    admin = User(
        id="user_a",
        team_id="team_a",
        email="admin@example.com",
    )

    assert (
        await approvals._get_scoped_approval(
            "approval_a",
            admin=admin,
            request=request,
            store=store,
            operation=SpaceOperation.READ_HISTORY,
        )
    ).id == "approval_a"
    assert (
        await approvals._get_scoped_approval(
            "approval_a",
            admin=admin,
            request=request,
            store=store,
            operation=SpaceOperation.EXECUTE,
        )
    ).id == "approval_a"

    user_store.status = "archived"
    assert (
        await approvals._get_scoped_approval(
            "approval_a",
            admin=admin,
            request=request,
            store=store,
            operation=SpaceOperation.READ_HISTORY,
        )
    ).id == "approval_a"

    with pytest.raises(HTTPException) as archived:
        await approvals.reject(
            "approval_a",
            approvals.DecideBody(),
            request,
            store,
            admin,
        )
    assert archived.value.status_code == 409
    assert archived.value.detail["error"]["code"] == "workspace_archived"
    assert store.decision_writes == 0

    user_store.removed.add("user_a")
    with pytest.raises(HTTPException) as removed:
        await approvals._get_scoped_approval(
            "approval_a",
            admin=admin,
            request=request,
            store=store,
            operation=SpaceOperation.READ_HISTORY,
        )
    assert removed.value.status_code == 403
