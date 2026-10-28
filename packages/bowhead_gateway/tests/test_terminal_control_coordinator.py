"""Production system-control coordinator fairness and denial semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.terminal_protocol import (
    DecisionAction,
    DecisionSourceKind,
    TerminalProtocolFutureVersionError,
)
from bowhead_core.terminal_protocol_store import (
    DecisionAccessRevokedError,
    DecisionAuthorizationDeniedError,
)
from bowhead_gateway.terminal_control_coordinator import (
    _read_rotating_pending_page,
    expire_due_terminal_decisions,
    revoke_inaccessible_terminal_decisions,
)
from bowhead_gateway.terminal_decision_authorization import (
    TerminalDecisionAuthorizer,
)


class _PendingStore:
    def __init__(self, items) -> None:
        self.items = list(items)
        self.calls: list[tuple[int, str | None]] = []

    async def list_v1_pending_candidates(
        self,
        *,
        limit: int,
        after_id: str | None,
    ):
        self.calls.append((limit, after_id))
        start = 0
        if after_id is not None:
            for index, item in enumerate(self.items):
                identity = getattr(item, "request_id", getattr(item, "id", ""))
                if identity == after_id:
                    start = index + 1
                    break
        return self.items[start : start + limit]


class _ExpiryStore:
    def __init__(self, items) -> None:
        self.items = list(items)
        self.calls: list[tuple[float, int]] = []

    async def list_v1_expiry_candidates(
        self,
        *,
        ttl_hours: float,
        limit: int,
    ):
        self.calls.append((ttl_hours, limit))
        return self.items[:limit]


class _ProtocolStore:
    async def read_persisted_turn_scope(self, *, turn_id: str):
        return "team_1", f"session:{turn_id}", 1


class _MappedProtocolStore:
    def __init__(self, versions) -> None:
        self.versions = versions

    async def read_persisted_turn_scope(self, *, turn_id: str):
        version = self.versions[turn_id]
        if isinstance(version, Exception):
            raise version
        return "team_1", f"session:{turn_id}", version


class _Factory:
    def __init__(self, *, fail_turn: str | None = None) -> None:
        self.fail_turn = fail_turn
        self.recorded: list[dict] = []
        self.committed: list[tuple[str, str, str]] = []

    async def record_source_decision(self, **kwargs):
        self.recorded.append(kwargs)
        return SimpleNamespace(turn_id=kwargs["turn_id"])

    async def commit_decision_control(
        self,
        prepared,
        *,
        reason_code: str,
        message_key: str,
    ) -> None:
        if prepared.turn_id == self.fail_turn:
            raise RuntimeError("injected first-row failure")
        self.committed.append((prepared.turn_id, reason_code, message_key))


def _candidate(index: int):
    return SimpleNamespace(
        request_id=f"clarify_{index}",
        turn_id=f"turn_{index}",
        workspace_id="ws_1",
        user_id="u_1",
    )


def _approval_candidate(index: int):
    return SimpleNamespace(
        id=f"approval_{index}",
        turn_id=f"approval_turn_{index}",
        workspace_id="ws_1",
        user_id="u_approval",
    )


def _app(items, *, factory: _Factory):
    return SimpleNamespace(
        state=SimpleNamespace(
            terminal_runtime_factory=factory,
            terminal_protocol_store=_ProtocolStore(),
            clarification_store=_PendingStore(items),
            approval_store=None,
        )
    )


def _expiry_app(
    *,
    clarifications=(),
    approvals=(),
    factory: _Factory | None = None,
    protocol_store=None,
):
    return SimpleNamespace(
        state=SimpleNamespace(
            terminal_runtime_factory=factory or _Factory(),
            terminal_protocol_store=protocol_store or _ProtocolStore(),
            clarification_store=_ExpiryStore(clarifications),
            approval_store=_ExpiryStore(approvals),
        )
    )


@pytest.mark.asyncio
async def test_expiry_coordinator_uses_typed_system_decisions_for_both_sources() -> None:
    factory = _Factory()
    app = _expiry_app(
        clarifications=[_candidate(1)],
        approvals=[_approval_candidate(1)],
        factory=factory,
    )

    completed = await expire_due_terminal_decisions(
        app,
        ttl_hours=12,
        limit=2,
    )

    assert completed == 2
    assert [row["source_kind"] for row in factory.recorded] == [
        DecisionSourceKind.CLARIFICATION,
        DecisionSourceKind.APPROVAL,
    ]
    assert all(row["action"] is DecisionAction.EXPIRE for row in factory.recorded)
    assert [row["principals"].actor_ref for row in factory.recorded] == [
        "system:clarification-expiry",
        "system:approval-expiry",
    ]
    assert factory.committed == [
        ("turn_1", "decision_expired", "turn.decision_expired"),
        (
            "approval_turn_1",
            "decision_expired",
            "turn.decision_expired",
        ),
    ]


@pytest.mark.asyncio
async def test_expiry_coordinator_skips_legacy_and_future_without_writes() -> None:
    factory = _Factory()
    app = _expiry_app(
        clarifications=[_candidate(1), _candidate(2)],
        factory=factory,
        protocol_store=_MappedProtocolStore(
            {
                "turn_1": None,
                "turn_2": TerminalProtocolFutureVersionError("future"),
            }
        ),
    )

    assert await expire_due_terminal_decisions(app, limit=2) == 0
    assert factory.recorded == []
    assert factory.committed == []


@pytest.mark.asyncio
async def test_expiry_coordinator_first_failure_does_not_starve_next() -> None:
    factory = _Factory(fail_turn="turn_1")
    app = _expiry_app(
        clarifications=[_candidate(1), _candidate(2)],
        factory=factory,
    )

    assert await expire_due_terminal_decisions(app, limit=2) == 1
    assert [row["turn_id"] for row in factory.recorded] == [
        "turn_1",
        "turn_2",
    ]
    assert factory.committed == [("turn_2", "decision_expired", "turn.decision_expired")]


@pytest.mark.asyncio
async def test_expiry_coordinator_never_commits_more_than_limit() -> None:
    factory = _Factory()
    clarification_store = _ExpiryStore([_candidate(1), _candidate(2), _candidate(3)])
    approval_store = _ExpiryStore([_approval_candidate(1)])
    app = SimpleNamespace(
        state=SimpleNamespace(
            terminal_runtime_factory=factory,
            terminal_protocol_store=_ProtocolStore(),
            clarification_store=clarification_store,
            approval_store=approval_store,
        )
    )

    assert await expire_due_terminal_decisions(app, limit=2) == 2
    assert len(factory.committed) == 2
    assert [turn_id for turn_id, _reason, _message in factory.committed] == [
        "turn_1",
        "approval_turn_1",
    ]
    assert approval_store.calls == [(24.0, 2)]


@pytest.mark.parametrize(
    ("denial_code", "expected_reason", "expected_message"),
    [
        ("entitlement_denied", "terminal_dependency_missing", "turn.failed"),
        ("dependency_missing", "terminal_dependency_missing", "turn.failed"),
        ("access_revoked", "access_revoked", "turn.access_revoked"),
    ],
)
@pytest.mark.asyncio
async def test_revoke_coordinator_maps_finite_denials(
    monkeypatch,
    denial_code: str,
    expected_reason: str,
    expected_message: str,
) -> None:
    async def deny(self, **_kwargs):
        raise DecisionAuthorizationDeniedError(denial_code)

    monkeypatch.setattr(
        TerminalDecisionAuthorizer,
        "authorize_pending_source",
        deny,
    )
    factory = _Factory()
    app = _app([_candidate(1)], factory=factory)

    completed = await revoke_inaccessible_terminal_decisions(
        app,
        limit=2,
    )

    assert completed == 1
    assert factory.committed == [("turn_1", expected_reason, expected_message)]
    assert factory.recorded[0]["source_values"] == {"denial_reason_code": denial_code}


@pytest.mark.asyncio
async def test_revoke_coordinator_first_row_failure_does_not_starve_next(
    monkeypatch,
) -> None:
    async def deny(self, **_kwargs):
        raise DecisionAccessRevokedError("revoked")

    monkeypatch.setattr(
        TerminalDecisionAuthorizer,
        "authorize_pending_source",
        deny,
    )
    factory = _Factory(fail_turn="turn_1")
    app = _app([_candidate(1), _candidate(2)], factory=factory)

    completed = await revoke_inaccessible_terminal_decisions(
        app,
        limit=4,
    )

    assert completed == 1
    assert [row["turn_id"] for row in factory.recorded] == [
        "turn_1",
        "turn_2",
    ]
    assert factory.committed == [("turn_2", "access_revoked", "turn.access_revoked")]


@pytest.mark.asyncio
async def test_rotating_page_wraps_once_without_duplicates() -> None:
    store = _PendingStore([_candidate(1), _candidate(2), _candidate(3)])

    page = await _read_rotating_pending_page(
        store.list_v1_pending_candidates,
        limit=2,
        after_id="clarify_3",
        identity=lambda item: item.request_id,
    )

    assert [item.request_id for item in page] == [
        "clarify_1",
        "clarify_2",
    ]
    assert store.calls == [(2, "clarify_3"), (2, None)]
