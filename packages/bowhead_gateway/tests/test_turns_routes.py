"""Turn replay HTTP 路由测试."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.terminal_effect_contract import EffectProbeEvidence
from bowhead_core.terminal_protocol import (
    EffectAttemptContext,
    EffectAttemptDetail,
    EffectClass,
    EffectSettlementOutcome,
    EffectSettlementSource,
    EffectState,
    GenerationState,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
    canonical_digest_v1,
)
from bowhead_core.terminal_protocol_store import (
    DecisionAccessRevokedError,
    DecisionSourceNotReadyError,
    TerminalProtocolEventRow,
    TerminalProtocolFutureVersionError,
)
from bowhead_core.user_store import User
from bowhead_gateway.dtos import TurnDTO
from bowhead_gateway.main import create_app
from bowhead_gateway.routes import turns as turns_routes
from bowhead_gateway.settings import Settings
from fastapi import HTTPException

from bowhead_core import TurnLogStore
from bowhead_core import metrics as core_metrics

if TYPE_CHECKING:
    from pathlib import Path


def test_turn_dto_preserves_generation_and_protocol_discriminator() -> None:
    payload = TurnDTO(
        id="t_generation_3",
        seq=1,
        user_text="resume",
        status="running",
        execution_generation=3,
        terminal_protocol_version=1,
        started_at="2026-08-14T00:00:00Z",
    ).model_dump()

    assert payload["execution_generation"] == 3
    assert payload["terminal_protocol_version"] == 1


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.turn_log_store = store
    app.state.orchestrator = None
    app.state.datarc = None
    return app


@pytest.mark.integration
async def test_list_events_404_for_unknown(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/turns/unknown/events")
        assert r.status_code == 404


@pytest.mark.integration
async def test_list_and_replay(app_ctx) -> None:
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(turn_id="t1", event_type="turn_started", payload={"user_text": "hi"})
    await store.record(turn_id="t1", event_type="headline", payload={"text": "样例"})
    await store.record(turn_id="t1", event_type="done", payload={"status": "completed"})

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/turns/t1/events")
        assert r.status_code == 200
        events = r.json()["events"]
        assert [e["event_type"] for e in events] == ["turn_started", "headline", "done"]

        # replay — SSE body (EventSourceResponse writes event: / data: into response).
        r = await c.get("/api/v1/turns/t1/replay")
        assert r.status_code == 200
        body = r.text
        assert "event: turn_started" in body
        assert "event: done" in body

        # list turn ids
        r = await c.get("/api/v1/turns")
        assert r.status_code == 200
        assert "t1" in r.json()["turn_ids"]


@pytest.mark.integration
async def test_v1_history_keeps_reader_reserved_marker_sequence_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(seq: int, event_type: str, payload: dict[str, object]):
        return TerminalProtocolEventRow(
            event_id=f"evt_{seq}",
            turn_id="t_v1",
            generation=1,
            event_seq=seq,
            event_type=event_type,
            actor_scope="root",
            phase_scope="in_generation",
            terminal_receipt_id=None,
            payload=payload,
            payload_digest=canonical_digest_v1(payload),
            occurred_at="2026-08-14T00:00:00Z",
        )

    class ProtocolStore(_V1ProtocolStore):
        async def read_auth_fence_vector(self, _context):
            return SimpleNamespace(auth_fence_vector_digest="auth_1")

        async def read_checkpoint(self, _context):
            return SimpleNamespace(
                current_generation=1,
                current_receipt_summary=None,
                persistent_event_highwater=3,
            )

        async def list_protocol_events(self, _context, *, up_to_event_seq=None):
            assert up_to_event_seq == 3
            return (
                row(1, "headline", {"text": "before"}),
                row(
                    2,
                    "turn.llm_invocation",
                    {"attempt_id": "eff_1", "protocol": 2},
                ),
                row(3, "step_started", {"step_id": "step_1"}),
            )

        async def read_current_effect_disclosure(self, _scope):
            return None

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    async def passthrough(_request, _turn_id, values, **_kwargs):
        return [payload for _event_type, payload in values]

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    monkeypatch.setattr(
        turns_routes,
        "project_event_payloads_for_viewer",
        passthrough,
    )
    app = create_app()
    app.state.settings = Settings()
    app.state.terminal_protocol_store = ProtocolStore()
    app.state.session_store = _V1SessionStore()
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        response = await client.get("/api/v1/turns/t_v1/events")

    assert response.status_code == 200
    body = response.json()
    assert [item["seq"] for item in body["events"]] == [1, 2, 3]
    assert [item["event_type"] for item in body["events"]] == [
        "headline",
        "turn.llm_invocation",
        "step_started",
    ]
    assert body["persistent_event_highwater"] == 3


@pytest.mark.integration
async def test_list_turns_uses_canonical_team_scope_in_authenticated_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id="u_admin",
        team_id="team_a",
        email="admin@example.com",
        email_verified=True,
    )

    class CanonicalStore:
        async def list_recent_turn_ids(self, **kwargs):
            assert kwargs == {
                "workspace_ids": ["ws_a"],
                "user_id": None,
                "limit": 200,
            }
            return ["t_new", "t_hidden"]

    class Users:
        async def get_team_role(self, user_id: str, team_id: str):
            assert (user_id, team_id) == ("u_admin", "team_a")
            return "team_admin"

        async def list_active_workspace_ids(self, team_id: str):
            assert team_id == "team_a"
            return ["ws_a"]

    class LegacyLog:
        async def list_turn_ids(self, *, limit: int):
            raise AssertionError("authenticated listing must not use legacy turn_events")

    async def current_user(_request):
        return user

    async def require_access(turn_id: str, *_args, **_kwargs):
        if turn_id == "t_hidden":
            raise HTTPException(404, "turn not found")
        return user

    monkeypatch.setattr(turns_routes, "_dev_user_fallback_enabled", lambda _request: False)
    monkeypatch.setattr(turns_routes, "_current_user", current_user)
    monkeypatch.setattr(turns_routes, "_require_turn_access", require_access)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=CanonicalStore(),
                user_store=Users(),
            ),
        ),
    )

    result = await turns_routes.list_turns(request, LegacyLog(), 200)

    assert result.turn_ids == ["t_new"]


# ── Feishu 07.06 #7: Stop cascades durable plan cancellation ─────────────────
class _StubPlanStore:
    def __init__(self, durable_turns: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.durable_turns = durable_turns or set()

    async def cancel_execution_for_turn(self, turn_id: str) -> bool:
        self.calls.append((turn_id, "durable_cancel_lookup"))
        return turn_id in self.durable_turns

    async def update_plan_status(self, plan_id: str, status) -> bool:
        self.calls.append((plan_id, str(status)))
        return True


class _StubOrch:
    def __init__(self, exec_map: dict[str, str], signaled: bool) -> None:
        self._m = exec_map
        self._signaled = signaled

    def signal_cancel(self, turn_id: str) -> bool:
        return self._signaled

    def get_executing_plan(self, turn_id: str):
        return self._m.get(turn_id)


class _StubSessionStore:
    def __init__(
        self,
        *,
        plan_cancelled: bool = False,
        cancel_outcome: str = "cancelled",
    ) -> None:
        self.plan_cancelled = plan_cancelled
        self.cancel_outcome = cancel_outcome

    async def get_turn(self, turn_id: str):
        return SimpleNamespace(id=turn_id, session_id="s_cancel", execution_generation=1)

    async def get_session(self, session_id: str):
        return SimpleNamespace(id=session_id, workspace_id="ws_1")

    async def request_cancel_turn(self, **kwargs):
        if kwargs.get("include_plan_outcome"):
            return self.cancel_outcome, self.plan_cancelled
        return self.cancel_outcome

    async def get_cancel_turn_receipt(self, **_kwargs):
        return SimpleNamespace(
            outcome=self.cancel_outcome,
            workspace_id="ws_1",
            turn_erased=False,
        )


_CANCEL_BODY = {
    "cancel_request_id": "eca80362-98ae-5b97-bb9b-fb435f5c27d0",
    "source": "explicit_stop",
    "expected_execution_generation": 1,
}


def _cancel_body_for(
    *,
    team_id: str,
    user_id: str,
    turn_id: str,
    execution_generation: int = 1,
) -> dict[str, object]:
    return {
        "cancel_request_id": str(
            turns_routes.explicit_stop_request_id(
                team_id=team_id,
                user_id=user_id,
                turn_id=turn_id,
                execution_generation=execution_generation,
            ),
        ),
        "source": "explicit_stop",
        "expected_execution_generation": execution_generation,
    }


def test_explicit_stop_uuidv5_golden() -> None:
    assert (
        str(
            turns_routes.explicit_stop_request_id(
                team_id="team_1",
                user_id="user_1",
                turn_id="turn_1",
                execution_generation=1,
            ),
        )
        == "eca80362-98ae-5b97-bb9b-fb435f5c27d0"
    )


@pytest.mark.integration
async def test_cancel_turn_cascades_plan_cancel(app_ctx) -> None:
    # Clicking Stop while turn executes a plan also durably cancels plan (CANCELLED), even if in-process
    # signal_cancel already returned False (turn outside tool-loop, plan still running in background).
    store = _StubPlanStore()
    app_ctx.state.orchestrator = _StubOrch({"t_exec": "plan_x"}, signaled=False)
    app_ctx.state.session_store = _StubSessionStore(plan_cancelled=True)
    app_ctx.state.plan_store = store
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/turns/t_exec/cancel", json=_CANCEL_BODY)
    assert r.status_code == 202
    body = r.json()
    assert body["plan_cancelled"] is True
    assert body["status"] == "cancelled"
    assert body["durable"] is True
    assert store.calls == []


@pytest.mark.integration
async def test_cancel_turn_cascades_plan_from_durable_cross_process_mapping(
    app_ctx,
) -> None:
    store = _StubPlanStore({"t_remote_exec"})
    app_ctx.state.orchestrator = _StubOrch({}, signaled=False)
    app_ctx.state.session_store = _StubSessionStore(plan_cancelled=True)
    app_ctx.state.plan_store = store
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_remote_exec/cancel",
            json=_CANCEL_BODY,
        )
    assert response.status_code == 202
    assert response.json()["plan_cancelled"] is True
    assert store.calls == []


@pytest.mark.integration
async def test_terminal_turn_with_repaired_plan_returns_noop(app_ctx) -> None:
    app_ctx.state.orchestrator = _StubOrch({}, signaled=True)
    app_ctx.state.session_store = _StubSessionStore(
        plan_cancelled=True,
        cancel_outcome="terminal",
    )
    app_ctx.state.plan_store = _StubPlanStore()
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/turns/t_terminal/cancel",
            json=_CANCEL_BODY,
        )

    assert response.status_code == 202
    assert response.json() == {
        "status": "noop",
        "ack": "rejected",
        "target_state": "present",
        "source": "explicit_stop",
        "turn_id": "t_terminal",
        "cancel_request_id": _CANCEL_BODY["cancel_request_id"],
        "execution_generation": 1,
        "durable": True,
        "reason": "turn_not_running",
        "plan_cancelled": True,
    }


@pytest.mark.integration
async def test_generation_conflict_returns_durable_rejected_receipt(app_ctx) -> None:
    app_ctx.state.orchestrator = _StubOrch({}, signaled=False)
    app_ctx.state.session_store = _StubSessionStore(
        cancel_outcome="generation_conflict",
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/turns/t_generation_changed/cancel",
            json=_CANCEL_BODY,
        )

    assert response.status_code == 202
    assert response.json() == {
        "status": "noop",
        "ack": "rejected",
        "target_state": "present",
        "source": "explicit_stop",
        "turn_id": "t_generation_changed",
        "cancel_request_id": _CANCEL_BODY["cancel_request_id"],
        "execution_generation": 1,
        "durable": True,
        "reason": "execution_generation_changed",
        "plan_cancelled": False,
    }


@pytest.mark.integration
async def test_cancel_turn_plain_no_plan_cascade(app_ctx) -> None:
    # Ordinary turn (not plan execution) → do not accidentally cancel plan.
    store = _StubPlanStore()
    app_ctx.state.orchestrator = _StubOrch({}, signaled=True)
    app_ctx.state.session_store = _StubSessionStore()
    app_ctx.state.plan_store = store
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/turns/t_plain/cancel", json=_CANCEL_BODY)
    assert r.status_code == 202
    assert r.json()["plan_cancelled"] is False
    assert r.json()["durable"] is True
    assert store.calls == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("receipt_outcome", "expected_status", "expected_ack", "expected_reason"),
    [
        ("cancelled", "cancelled", "accepted", None),
        ("terminal", "noop", "rejected", "turn_not_running"),
        (
            "generation_conflict",
            "noop",
            "rejected",
            "execution_generation_changed",
        ),
    ],
)
async def test_cancel_receipt_get_reconciles_unknown_post_response(
    app_ctx,
    receipt_outcome: str,
    expected_status: str,
    expected_ack: str,
    expected_reason: str | None,
) -> None:
    app_ctx.state.session_store = _StubSessionStore(
        cancel_outcome=receipt_outcome,
    )
    app_ctx.state.plan_store = _StubPlanStore({"t_receipt"})
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get(
            f"/api/v1/turns/t_receipt/cancel/{_CANCEL_BODY['cancel_request_id']}",
            params={"expected_execution_generation": 1},
        )

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    assert response.json()["ack"] == expected_ack
    assert response.json().get("reason") == expected_reason
    assert response.json()["durable"] is True


@pytest.mark.asyncio
async def test_get_cancelled_erased_receipt_repairs_plan_without_post(
    monkeypatch,
) -> None:
    class _ErasedTurnStore:
        async def get_turn(self, _turn_id: str):
            return None

        async def get_cancel_turn_receipt(self, **_kwargs):
            return SimpleNamespace(
                outcome="cancelled",
                turn_erased=True,
                workspace_id="ws_1",
            )

    async def current_user(_request):
        return SimpleNamespace(id="u_1", team_id="team_1")

    async def allow_current_space(*_args, **_kwargs):
        return None

    monkeypatch.setattr(turns_routes, "_dev_user_fallback_enabled", lambda _request: False)
    monkeypatch.setattr(turns_routes, "_current_user", current_user)
    monkeypatch.setattr(
        turns_routes,
        "build_access_context_for_user_from_request",
        allow_current_space,
    )
    plan_store = _StubPlanStore({"t_erased"})
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_ErasedTurnStore(),
                plan_store=plan_store,
            ),
        ),
    )
    body = _cancel_body_for(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_erased",
    )

    replay = await turns_routes.get_cancel_turn_outcome(
        "t_erased",
        turns_routes.CancelTurnRequest.model_validate(body).cancel_request_id,
        request,  # type: ignore[arg-type]
        expected_execution_generation=1,
    )

    assert replay["ack"] == "accepted"
    assert replay["target_state"] == "erased"
    assert plan_store.calls == [("t_erased", "durable_cancel_lookup")]


@pytest.mark.asyncio
async def test_cancel_turn_replays_receipt_after_turn_erasure(monkeypatch) -> None:
    class _ErasedTurnStore:
        repaired = False

        async def get_turn(self, _turn_id: str):
            return None

        async def get_cancel_turn_receipt(self, **_kwargs):
            return SimpleNamespace(
                outcome="cancelled",
                turn_erased=True,
                workspace_id="ws_1",
            )

        async def request_cancel_turn(self, **kwargs):
            assert kwargs["include_plan_outcome"] is True
            self.repaired = True
            return "cancelled", True

    async def current_user(_request):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_dev_user_fallback_enabled", lambda _request: False)
    monkeypatch.setattr(turns_routes, "_current_user", current_user)

    async def allow_current_space(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        turns_routes,
        "build_access_context_for_user_from_request",
        allow_current_space,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_ErasedTurnStore(),
                orchestrator=None,
                plan_store=_StubPlanStore({"t_erased"}),
            ),
        ),
    )

    erased_body = _cancel_body_for(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_erased",
    )
    parsed_body = turns_routes.CancelTurnRequest.model_validate(erased_body)
    response = await turns_routes.cancel_turn(
        "t_erased",
        parsed_body,
        request,  # type: ignore[arg-type]
    )

    assert response == {
        "status": "cancelled",
        "ack": "accepted",
        "target_state": "erased",
        "source": "explicit_stop",
        "turn_id": "t_erased",
        "cancel_request_id": erased_body["cancel_request_id"],
        "execution_generation": 1,
        "durable": True,
        "plan_cancelled": True,
    }
    replay = await turns_routes.get_cancel_turn_outcome(
        "t_erased",
        parsed_body.cancel_request_id,
        request,  # type: ignore[arg-type]
        expected_execution_generation=1,
    )
    assert replay["ack"] == "accepted"
    assert replay["target_state"] == "erased"


@pytest.mark.asyncio
async def test_erased_cancel_receipt_replay_rechecks_current_space_access(
    monkeypatch,
) -> None:
    class _ErasedTurnStore:
        async def get_turn(self, _turn_id: str):
            return None

        async def get_cancel_turn_receipt(self, **_kwargs):
            return SimpleNamespace(
                outcome="cancelled",
                turn_erased=True,
                workspace_id="ws_revoked",
            )

    async def current_user(_request):
        return SimpleNamespace(id="u_1", team_id="team_1")

    checked: list[str] = []

    async def deny_current_space(_request, *, workspace_id: str, **_kwargs):
        checked.append(workspace_id)
        raise HTTPException(403, {"error": {"code": "space_access_denied"}})

    monkeypatch.setattr(turns_routes, "_dev_user_fallback_enabled", lambda _request: False)
    monkeypatch.setattr(turns_routes, "_current_user", current_user)
    monkeypatch.setattr(
        turns_routes,
        "build_access_context_for_user_from_request",
        deny_current_space,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_ErasedTurnStore(),
                orchestrator=None,
            ),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        erased_body = _cancel_body_for(
            team_id="team_1",
            user_id="u_1",
            turn_id="t_erased",
        )
        await turns_routes.cancel_turn(
            "t_erased",
            turns_routes.CancelTurnRequest.model_validate(erased_body),
            request,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 403
    assert checked == ["ws_revoked"]


class _V1ProtocolStore:
    def __init__(
        self,
        *,
        status: TerminalStatus | None = None,
        reason_code: str | None = None,
        future: bool = False,
    ) -> None:
        self.status = status
        self.reason_code = reason_code
        self.future = future
        self.scope_reads = 0
        self.generation = 1
        self.generation_kind = "execution"

    async def read_persisted_turn_scope(self, *, turn_id: str):
        assert turn_id == "t_v1"
        self.scope_reads += 1
        if self.future:
            raise TerminalProtocolFutureVersionError("future")
        return "team_1", "s_1", 1

    async def read_protocol_summary(self, context):
        assert (
            context.team_id,
            context.session_id,
            context.turn_id,
            context.viewer,
            context.auth_fence_vector_digest,
        ) == ("team_1", "s_1", "t_v1", "u_1", "auth_n")
        receipt = (
            SimpleNamespace(
                status=self.status,
                reason_code=self.reason_code,
            )
            if self.status is not None
            else None
        )
        return SimpleNamespace(
            version=1,
            current_generation=self.generation,
            current_generation_kind=self.generation_kind,
            current_generation_closing_reason=(
                self.reason_code
                if self.generation_kind == "control"
                else "explicit_stop_cancelled/v1"
                if self.status is TerminalStatus.CANCELLED
                else None
            ),
            current_generation_state=(GenerationState.CLOSED if receipt is not None else GenerationState.RUNNING),
            current_generation_resolution=None,
            current_receipt=receipt,
        )

    async def read_persisted_turn_protocol_binding(
        self,
        *,
        team_id: str,
        turn_id: str,
    ):
        assert (team_id, turn_id) == ("team_1", "t_v1")
        return "s_1", 1


class _V1SessionStore:
    def __init__(self, *, active: bool = True, fail_receipt: bool = False) -> None:
        self.active = active
        self.fail_receipt = fail_receipt
        self.cancel_receipts: dict[str, SimpleNamespace] = {}

    async def get_session(self, session_id: str):
        assert session_id == "s_1"
        return SimpleNamespace(
            id="s_1",
            user_id="u_1",
            workspace_id="ws_1",
            deleted_at=None,
        )

    async def get_active_turn_identity(self, session_id: str):
        assert session_id == "s_1"
        return ("t_v1", 1) if self.active else (None, None)

    async def get_turn(self, turn_id: str):
        assert turn_id == "t_v1"
        return SimpleNamespace(
            id="t_v1",
            session_id="s_1",
            execution_generation=1,
            terminal_protocol_version=1,
        )

    async def record_terminal_cancel_receipt(self, **kwargs):
        if self.fail_receipt:
            raise RuntimeError("cancel receipt store unavailable")
        request_id = kwargs["cancel_request_id"]
        existing = self.cancel_receipts.get(request_id)
        if existing is not None:
            if existing.outcome != kwargs["outcome"]:
                raise ValueError("cancel_request_conflict")
            return existing.outcome
        self.cancel_receipts[request_id] = SimpleNamespace(
            outcome=kwargs["outcome"],
            workspace_id=kwargs["workspace_id"],
            turn_erased=False,
        )
        return kwargs["outcome"]

    async def get_cancel_turn_receipt(self, **kwargs):
        return self.cancel_receipts.get(kwargs["cancel_request_id"])


class _V1UserStore:
    async def get_user(self, user_id: str):
        assert user_id == "u_1"
        return User(
            id="u_1",
            team_id="team_1",
            email="u_1@example.test",
            email_verified=True,
            tier="ultra",
            status="active",
            credit_balance=100,
            created_at="2026-08-03T00:00:00Z",
        )

    async def get_team_role(self, user_id: str, team_id: str):
        assert (user_id, team_id) == ("u_1", "team_1")
        return "member"

    async def get_workspace_team_id(self, workspace_id: str):
        assert workspace_id == "ws_1"
        return "team_1"

    async def get_member_role(self, workspace_id: str, user_id: str):
        assert (workspace_id, user_id) == ("ws_1", "u_1")
        return "member"

    async def get_workspace(self, workspace_id: str):
        assert workspace_id == "ws_1"
        return {"id": "ws_1", "team_id": "team_1", "status": "active"}


class _V1Factory:
    def __init__(
        self,
        protocol_store: _V1ProtocolStore,
        *,
        events: list[str],
        fail_commit: bool = False,
    ) -> None:
        self.protocol_store = protocol_store
        self.events = events
        self.fail_commit = fail_commit
        self.authorize_calls = 0
        self.recovery_calls = 0
        self.record_calls = 0
        self.commit_calls = 0

    async def prepare_web_auth_fence(self, **_kwargs) -> str:
        return "auth_n"

    async def authorize_active_web_control(self, **kwargs):
        self.authorize_calls += 1
        assert kwargs["auth_fence_vector_digest"] == "auth_n"
        return TerminalScope(
            team_id="team_1",
            session_id="s_1",
            turn_id="t_v1",
            generation=1,
        )

    async def trigger_recovery(self, *_args, **_kwargs) -> None:
        self.recovery_calls += 1

    async def commit_active_web_cancel(self, scope: TerminalScope):
        self.commit_calls += 1
        self.events.append("commit")
        assert scope == TerminalScope("team_1", "s_1", "t_v1", 1)
        if self.fail_commit:
            raise DecisionSourceNotReadyError("commit unavailable")
        self.protocol_store.status = TerminalStatus.CANCELLED
        return SimpleNamespace(status=TerminalStatus.CANCELLED)

    async def record_source_decision(self, **_kwargs):
        self.record_calls += 1
        self.events.append("record")
        return SimpleNamespace(decision_id="decision_cancel")

    async def commit_decision_control(self, _prepared, **_kwargs):
        self.commit_calls += 1
        self.events.append("commit")
        if self.fail_commit:
            raise DecisionSourceNotReadyError("commit unavailable")
        self.protocol_store.generation += 1
        self.protocol_store.status = TerminalStatus.CANCELLED


class _V1Orchestrator:
    def __init__(
        self,
        *,
        registered_generation: int,
        events: list[str],
        fail_signal: bool = False,
    ) -> None:
        self.registered_generation = registered_generation
        self.events = events
        self.fail_signal = fail_signal
        self.signals: list[tuple[str, int]] = []

    def signal_cancel(self, _turn_id: str) -> bool:
        raise AssertionError("v1 cancel must not use the legacy turn-only signal")

    def signal_cancel_generation(self, turn_id: str, generation: int) -> bool:
        if self.fail_signal:
            raise RuntimeError("generation signal unavailable")
        if generation != self.registered_generation:
            return False
        self.events.append("signal")
        self.signals.append((turn_id, generation))
        return True


class _V1PlanStore:
    def __init__(self, *, running: bool = False) -> None:
        self.running_turns = {"t_v1"} if running else set()
        self.calls: list[str] = []

    async def cancel_execution_for_turn(self, turn_id: str) -> bool:
        self.calls.append(turn_id)
        if turn_id not in self.running_turns:
            return False
        self.running_turns.remove(turn_id)
        return True


def _v1_cancel_app(
    *,
    registered_generation: int,
    active: bool = True,
    terminal_status: TerminalStatus | None = None,
    terminal_reason_code: str | None = None,
    future: bool = False,
    fail_commit: bool = False,
    fail_signal: bool = False,
    fail_receipt: bool = False,
    plan_running: bool = False,
    user_store=None,
):
    app = create_app()
    app.state.settings = Settings()
    app.state.protocol_store = None
    protocol_store = _V1ProtocolStore(
        status=terminal_status,
        reason_code=terminal_reason_code,
        future=future,
    )
    app.state.terminal_protocol_store = protocol_store
    app.state.session_store = _V1SessionStore(
        active=active,
        fail_receipt=fail_receipt,
    )
    app.state.user_store = user_store or _V1UserStore()
    app.state.access_control_store = None
    events: list[str] = []
    factory = _V1Factory(
        protocol_store,
        events=events,
        fail_commit=fail_commit,
    )
    orchestrator = _V1Orchestrator(
        registered_generation=registered_generation,
        events=events,
        fail_signal=fail_signal,
    )
    app.state.terminal_runtime_factory = factory
    app.state.orchestrator = orchestrator
    app.state.plan_store = _V1PlanStore(running=plan_running)
    return app, factory, orchestrator


@pytest.mark.integration
async def test_cancel_active_v1_signals_exact_authorized_generation(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(registered_generation=1)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1"),
        )

    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"
    assert response.json()["durable"] is True
    assert factory.authorize_calls == 1
    assert factory.recovery_calls == 0
    assert factory.record_calls == 0
    assert factory.commit_calls == 1
    assert factory.events == ["commit", "signal"]
    assert orchestrator.signals == [("t_v1", 1)]


@pytest.mark.integration
async def test_cancel_active_v1_get_replays_accepted_receipt(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, _factory, _orchestrator = _v1_cancel_app(registered_generation=1)
    body = _cancel_body_for(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_v1",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        posted = await c.post("/api/v1/turns/t_v1/cancel", json=body)
        replayed = await c.get(
            f"/api/v1/turns/t_v1/cancel/{body['cancel_request_id']}",
            params={"expected_execution_generation": 1},
        )

    assert posted.status_code == 202
    assert replayed.status_code == 200
    assert replayed.json() == {
        "status": "cancelled",
        "ack": "accepted",
        "target_state": "present",
        "source": "explicit_stop",
        "turn_id": "t_v1",
        "cancel_request_id": body["cancel_request_id"],
        "execution_generation": 1,
        "durable": True,
    }


@pytest.mark.integration
async def test_cancel_active_v1_get_replay_repairs_running_plan(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        plan_running=True,
    )
    body = _cancel_body_for(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_v1",
    )
    app.state.session_store.cancel_receipts[body["cancel_request_id"]] = SimpleNamespace(
        outcome="cancelled",
        workspace_id="ws_1",
        turn_erased=False,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        replayed = await c.get(
            f"/api/v1/turns/t_v1/cancel/{body['cancel_request_id']}",
            params={"expected_execution_generation": 1},
        )

    assert replayed.status_code == 200
    assert replayed.json()["status"] == "cancelled"
    assert app.state.plan_store.running_turns == set()
    assert app.state.plan_store.calls == ["t_v1"]
    assert factory.authorize_calls == 0
    assert factory.commit_calls == 0
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_active_v1_post_replay_repairs_running_plan(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        plan_running=True,
    )
    body = _cancel_body_for(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_v1",
    )
    app.state.session_store.cancel_receipts[body["cancel_request_id"]] = SimpleNamespace(
        outcome="cancelled",
        workspace_id="ws_1",
        turn_erased=False,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        replayed = await c.post("/api/v1/turns/t_v1/cancel", json=body)

    assert replayed.status_code == 202
    assert replayed.json()["status"] == "cancelled"
    assert replayed.json()["plan_cancelled"] is True
    assert app.state.plan_store.running_turns == set()
    assert app.state.plan_store.calls == ["t_v1"]
    assert factory.authorize_calls == 0
    assert factory.commit_calls == 0
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_paused_v1_ack_loss_recovers_cancelled_successor(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        active=False,
        terminal_status=TerminalStatus.CANCELLED,
        terminal_reason_code="user_cancelled",
        plan_running=True,
    )
    app.state.terminal_protocol_store.generation = 2
    app.state.terminal_protocol_store.generation_kind = "control"
    body = _cancel_body_for(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_v1",
        execution_generation=1,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        replayed = await c.post("/api/v1/turns/t_v1/cancel", json=body)

    assert replayed.status_code == 202
    assert replayed.json()["status"] == "cancelled"
    assert replayed.json()["ack"] == "accepted"
    assert replayed.json()["plan_cancelled"] is True
    assert app.state.plan_store.running_turns == set()
    assert app.state.plan_store.calls == ["t_v1"]
    assert app.state.session_store.cancel_receipts[body["cancel_request_id"]].outcome == "cancelled"
    assert factory.authorize_calls == 0
    assert factory.commit_calls == 0
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_stale_cancel_never_claims_cancelled_execution_successor(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=2,
        active=False,
        terminal_status=TerminalStatus.CANCELLED,
        plan_running=True,
    )
    app.state.terminal_protocol_store.generation = 2
    app.state.terminal_protocol_store.generation_kind = "execution"
    body = _cancel_body_for(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_v1",
        execution_generation=1,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post("/api/v1/turns/t_v1/cancel", json=body)

    assert response.status_code == 202
    assert response.json()["status"] == "noop"
    assert response.json()["ack"] == "rejected"
    assert response.json()["reason"] == "execution_generation_changed"
    assert response.json()["plan_cancelled"] is False
    assert app.state.plan_store.running_turns == {"t_v1"}
    assert app.state.plan_store.calls == []
    assert app.state.session_store.cancel_receipts[body["cancel_request_id"]].outcome == "generation_conflict"
    assert factory.authorize_calls == 0
    assert factory.commit_calls == 0
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_effect_disclosure_route_returns_only_content_free_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[str, float]] = []
    monkeypatch.setattr(
        core_metrics,
        "observe",
        lambda name, value, **_kwargs: observations.append((name, value)),
    )

    class ProtocolStore:
        async def read_current_effect_disclosure(self, scope):
            assert scope == TerminalScope("team_1", "s_1", "t_v1", 2)
            return SimpleNamespace(
                unknown_count=1,
                oldest_unknown_age_upper_bound_seconds=12,
                payload={
                    "attempt_count": 2,
                    "counts": {
                        "accounting": {
                            "applied": 0,
                            "compensated": 0,
                            "not_applied": 0,
                            "unsettled": 0,
                        },
                        "inference_internal": {
                            "applied": 1,
                            "compensated": 0,
                            "not_applied": 0,
                            "unsettled": 0,
                        },
                        "user_action": {
                            "applied": 0,
                            "compensated": 0,
                            "not_applied": 0,
                            "unsettled": 1,
                        },
                    },
                    "finalized": False,
                    "oldest_unknown_age_upper_bound_seconds": 12,
                    "oldest_unknown_attempt_started_at": ("2026-08-14T00:00:00Z"),
                    "proof_digest": "proof_1",
                    "protocol": 2,
                    "receipt_id": "receipt_1",
                    "unknown_count": 1,
                    "warning_message_keys": ["turn.effect_user_action_unsettled"],
                },
            )

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    async def scope(*_args, **_kwargs):
        return ProtocolStore(), TerminalScope("team_1", "s_1", "t_v1", 2)

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    monkeypatch.setattr(turns_routes, "_effect_scope_for_turn", scope)
    app = create_app()
    app.state.settings = Settings()
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/api/v1/turns/t_v1/effect-disclosure")

    assert response.status_code == 200
    assert response.json()["current_generation"] == 2
    disclosure = response.json()["current_effect_disclosure"]
    assert disclosure["unknown_count"] == 1
    assert disclosure["counts"]["user_action"]["unsettled"] == 1
    assert "provider" not in disclosure
    assert "evidence" not in disclosure
    assert observations == [
        ("bowhead_terminal_effect_unknown_count", 1),
        ("bowhead_terminal_effect_unknown_oldest_age_seconds", 12),
    ]


def _effect_attempt_detail(
    scope: TerminalScope,
    *,
    settlement=None,
) -> EffectAttemptDetail:
    settled = settlement is not None
    return EffectAttemptDetail(
        attempt=EffectAttemptContext(
            scope=scope,
            attempt_id="eff_1",
            action_ref="tool-action:1",
            provider="tool_external/v1",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            state=EffectState.SETTLED if settled else EffectState.UNKNOWN,
            claim_epoch=3,
            row_version=5 if settled else 4,
            idempotency_or_fence_token="provider-token-1",
            non_retriable=False,
        ),
        disclosure_kind="user_action",
        outcome=settlement.outcome if settled else None,
        result_digest=(
            canonical_digest_v1(
                {
                    "outcome": settlement.outcome.value,
                    "settlement_code": settlement.settlement_code,
                }
            )
            if settled
            else None
        ),
        settlement_id=settlement.settlement_id if settled else None,
        settlement_source=settlement.source if settled else None,
        settlement_code=settlement.settlement_code if settled else None,
        actor_audit_ref=settlement.principal_ref if settled else None,
        authz_snapshot_digest=(settlement.authz_snapshot_digest if settled else None),
        evidence_link_id=settlement.evidence_link_id if settled else None,
        evidence_digest=settlement.evidence_digest if settled else None,
        object_version_digest=(settlement.object_version_digest if settled else None),
        audit_object_ref=settlement.audit_object_ref if settled else None,
        evidence_retention_state="active" if settled else None,
        evidence_purge_after="2026-09-13T00:00:00Z" if settled else None,
        provider_external_ref_digest=(settlement.provider_external_ref_digest if settled else None),
        provider_external_ref_key_version=(settlement.provider_external_ref_key_version if settled else None),
        attempt_started_at="2026-08-14T00:00:00Z",
        manual_settlement_allowed=True,
    )


@pytest.mark.integration
async def test_effect_attempt_details_are_admin_only_and_revocation_is_opaque(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_value = TerminalScope("team_1", "s_1", "t_v1", 2)

    class ProtocolStore:
        revoked = False

        async def read_effect_attempt_details(self, scope, *, actor_user_id):
            assert (scope, actor_user_id) == (scope_value, "u_admin")
            if self.revoked:
                raise DecisionAccessRevokedError("membership changed")
            return (_effect_attempt_detail(scope_value),)

    class Users:
        role = "team_admin"
        unavailable = False

        async def get_team_role(self, user_id: str, team_id: str):
            assert (user_id, team_id) == ("u_admin", "team_1")
            if self.unavailable:
                raise RuntimeError("role store unavailable")
            return self.role

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_admin", team_id="team_1")

    protocol_store = ProtocolStore()
    users = Users()

    async def scope(*_args, **_kwargs):
        return protocol_store, scope_value

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    monkeypatch.setattr(turns_routes, "_effect_scope_for_turn", scope)
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = users
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/api/v1/turns/t_v1/effect-attempts")
        users.role = "member"
        non_admin = await client.get("/api/v1/turns/t_v1/effect-attempts")
        users.role = "team_admin"
        protocol_store.revoked = True
        revoked = await client.get("/api/v1/turns/t_v1/effect-attempts")
        protocol_store.revoked = False
        users.unavailable = True
        unavailable = await client.get("/api/v1/turns/t_v1/effect-attempts")

    assert response.status_code == 200
    assert response.json()["attempts"][0]["attempt_id"] == "eff_1"
    assert response.json()["attempts"][0]["provider_contract_key"] == ("tool_external/v1")
    assert non_admin.status_code == 404
    assert revoked.status_code == 404
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["error"]["code"] == ("terminal_dependency_recovering")


@pytest.mark.integration
async def test_manual_effect_settlement_replays_after_auth_epoch_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_value = TerminalScope("team_1", "s_1", "t_v1", 2)

    class ProtocolStore:
        settlement = None
        vector_reads = 0
        record_calls = 0

        async def read_auth_fence_vector(self, _context):
            self.vector_reads += 1
            return SimpleNamespace(auth_fence_vector_digest=("auth_old" if self.vector_reads == 1 else "auth_new"))

        async def read_effect_attempt_details(self, scope, *, actor_user_id):
            assert (scope, actor_user_id) == (scope_value, "u_admin")
            return (_effect_attempt_detail(scope_value, settlement=self.settlement),)

        async def record_manual_effect_settlement(
            self,
            context,
            *,
            actor_user_id,
            resolve_context,
            expected_auth_fence_vector,
        ):
            assert actor_user_id == "u_admin"
            assert resolve_context.team_id == "team_1"
            self.record_calls += 1
            if self.settlement is None:
                assert context.authz_snapshot_digest == "auth_old"
                self.settlement = context
            else:
                assert expected_auth_fence_vector.auth_fence_vector_digest == ("auth_new")
                assert context.authz_snapshot_digest == "auth_old"
                assert context == self.settlement
            return SimpleNamespace(
                attempt_id=context.attempt_id,
                row_version=5,
                state=EffectState.SETTLED,
            )

    class Users:
        async def get_team_role(self, user_id: str, team_id: str):
            assert (user_id, team_id) == ("u_admin", "team_1")
            return "owner"

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_admin", team_id="team_1")

    protocol_store = ProtocolStore()

    async def scope(*_args, **_kwargs):
        return protocol_store, scope_value

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    monkeypatch.setattr(turns_routes, "_effect_scope_for_turn", scope)
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = Users()
    body = {
        "attempt_id": "eff_1",
        "expected_attempt_row_version": 4,
        "outcome": "confirmed_not_applied",
        "settlement_code": "admin_attested_not_applied",
        "evidence_digest": "manual-evidence-1",
        "object_version_digest": "manual-object-v1",
    }
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        extra = await client.post(
            "/api/v1/turns/t_v1/effect-settlements/manual",
            json={**body, "unregistered_evidence": "ignored-before"},
        )
        first = await client.post(
            "/api/v1/turns/t_v1/effect-settlements/manual",
            json=body,
        )
        replay = await client.post(
            "/api/v1/turns/t_v1/effect-settlements/manual",
            json=body,
        )

    assert extra.status_code == 422
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert protocol_store.record_calls == 2


@pytest.mark.integration
async def test_manual_effect_settlement_conflict_maps_to_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_value = TerminalScope("team_1", "s_1", "t_v1", 2)

    class ProtocolStore:
        revoked = False

        async def read_auth_fence_vector(self, _context):
            return SimpleNamespace(auth_fence_vector_digest="auth_1")

        async def read_effect_attempt_details(self, _scope, *, actor_user_id):
            assert actor_user_id == "u_admin"
            return (_effect_attempt_detail(scope_value),)

        async def record_manual_effect_settlement(self, *_args, **_kwargs):
            if self.revoked:
                raise DecisionAccessRevokedError("admin authority changed")
            raise TerminalProtocolMalformedError("winner conflict")

    class Users:
        async def get_team_role(self, _user_id: str, _team_id: str):
            return "team_admin"

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_admin", team_id="team_1")

    protocol_store = ProtocolStore()

    async def scope(*_args, **_kwargs):
        return protocol_store, scope_value

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    monkeypatch.setattr(turns_routes, "_effect_scope_for_turn", scope)
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = Users()
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/api/v1/turns/t_v1/effect-settlements/manual",
            json={
                "attempt_id": "eff_1",
                "expected_attempt_row_version": 4,
                "outcome": "confirmed_not_applied",
                "settlement_code": "admin_attested_not_applied",
                "evidence_digest": "manual-evidence-1",
                "object_version_digest": "manual-object-v1",
            },
        )
        protocol_store.revoked = True
        revoked = await client.post(
            "/api/v1/turns/t_v1/effect-settlements/manual",
            json={
                "attempt_id": "eff_1",
                "expected_attempt_row_version": 4,
                "outcome": "confirmed_not_applied",
                "settlement_code": "admin_attested_not_applied",
                "evidence_digest": "manual-evidence-1",
                "object_version_digest": "manual-object-v1",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == ("effect_settlement_conflict")
    assert revoked.status_code == 404


@pytest.mark.integration
async def test_effect_probe_uses_registered_adapter_and_provider_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_value = TerminalScope("team_1", "s_1", "t_v1", 2)

    class ProtocolStore:
        def __init__(self) -> None:
            self.settlement = None

        async def read_auth_fence_vector(self, _context):
            return SimpleNamespace(auth_fence_vector_digest="auth_probe_1")

        async def read_effect_attempt_details(self, scope, *, actor_user_id):
            assert (scope, actor_user_id) == (scope_value, "u_admin")
            settled = self.settlement is not None
            return (
                EffectAttemptDetail(
                    attempt=EffectAttemptContext(
                        scope=scope_value,
                        attempt_id="eff_1",
                        action_ref="tool-action:1",
                        provider="tool_external/v1",
                        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
                        state=(EffectState.SETTLED if settled else EffectState.UNKNOWN),
                        claim_epoch=3,
                        row_version=5 if settled else 4,
                        idempotency_or_fence_token="provider-token-1",
                        non_retriable=False,
                    ),
                    disclosure_kind="user_action",
                    outcome=(EffectSettlementOutcome.CONFIRMED_APPLIED if settled else None),
                    result_digest="result_1" if settled else None,
                    settlement_id="set_1" if settled else None,
                    settlement_source=(EffectSettlementSource.PROVIDER if settled else None),
                    settlement_code="provider_receipt_found" if settled else None,
                    actor_audit_ref="adapter:tool_external_probe/v1" if settled else None,
                    authz_snapshot_digest="probe:tool_external_probe/v1" if settled else None,
                    evidence_link_id="evidence_1" if settled else None,
                    evidence_digest="evidence_digest_1" if settled else None,
                    object_version_digest="object_digest_1" if settled else None,
                    audit_object_ref="provider-audit:1" if settled else None,
                    evidence_retention_state="active" if settled else None,
                    evidence_purge_after=("2026-09-13T00:00:00Z" if settled else None),
                    provider_external_ref_digest=("provider_ref_digest_1" if settled else None),
                    provider_external_ref_key_version="v1" if settled else None,
                    attempt_started_at="2026-08-14T00:00:00Z",
                    manual_settlement_allowed=True,
                ),
            )

        async def record_admin_probed_effect_settlement(
            self,
            context,
            *,
            actor_user_id,
            resolve_context,
            expected_auth_fence_vector,
        ):
            assert actor_user_id == "u_admin"
            assert resolve_context.team_id == "team_1"
            assert expected_auth_fence_vector.auth_fence_vector_digest == ("auth_probe_1")
            assert context.purge_after is None
            if self.settlement is not None:
                assert context.settlement_id == self.settlement.settlement_id
                assert context.evidence_digest == self.settlement.evidence_digest
                assert context.object_version_digest == self.settlement.object_version_digest
                assert context.expected_attempt_row_version == 4
            self.settlement = context
            return SimpleNamespace(
                attempt_id=context.attempt_id,
                row_version=context.expected_attempt_row_version + 1,
                state=EffectState.SETTLED,
            )

    class Users:
        async def get_team_role(self, user_id: str, team_id: str):
            assert (user_id, team_id) == ("u_admin", "team_1")
            return "team_admin"

    protocol_store = ProtocolStore()
    probe_calls = 0

    async def probe(attempt):
        nonlocal probe_calls
        probe_calls += 1
        assert attempt.attempt_id == "eff_1"
        assert attempt.action_ref == "tool-action:1"
        assert attempt.idempotency_or_fence_token == "provider-token-1"
        return EffectProbeEvidence(
            outcome=EffectSettlementOutcome.CONFIRMED_APPLIED,
            settlement_code="provider_receipt_found",
            evidence_digest="evidence_digest_1",
            object_version_digest="object_digest_1",
            audit_object_ref="provider-audit:1",
            provider_external_ref_digest="provider_ref_digest_1",
            provider_external_ref_key_version="v1",
        )

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_admin", team_id="team_1")

    async def scope(*_args, **_kwargs):
        return protocol_store, scope_value

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    monkeypatch.setattr(turns_routes, "_effect_scope_for_turn", scope)
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = Users()
    app.state.terminal_effect_probe_adapters = {
        "tool_external_probe/v1": probe,
    }
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/api/v1/turns/t_v1/effect-attempts/eff_1/probe")
        replay = await client.post("/api/v1/turns/t_v1/effect-attempts/eff_1/probe")

    assert response.status_code == 200
    assert response.json() == {
        "attempt_id": "eff_1",
        "row_version": 5,
        "state": "settled",
    }
    assert replay.status_code == 200
    assert replay.json() == response.json()
    assert probe_calls == 1
    assert protocol_store.settlement.source is EffectSettlementSource.PROVIDER
    assert protocol_store.settlement.principal_ref == "adapter:tool_external_probe/v1"


@pytest.mark.integration
async def test_effect_probe_adapter_failure_is_retryable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_value = TerminalScope("team_1", "s_1", "t_v1", 2)

    class ProtocolStore:
        async def read_auth_fence_vector(self, _context):
            return SimpleNamespace(auth_fence_vector_digest="auth_probe_1")

        async def read_effect_attempt_details(self, _scope, *, actor_user_id):
            assert actor_user_id == "u_admin"
            return (_effect_attempt_detail(scope_value),)

    class Users:
        async def get_team_role(self, _user_id: str, _team_id: str):
            return "team_admin"

    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_admin", team_id="team_1")

    async def scope(*_args, **_kwargs):
        return ProtocolStore(), scope_value

    async def unavailable_probe(_attempt):
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    monkeypatch.setattr(turns_routes, "_effect_scope_for_turn", scope)
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = Users()
    app.state.terminal_effect_probe_adapters = {
        "tool_external_probe/v1": unavailable_probe,
    }
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/api/v1/turns/t_v1/effect-attempts/eff_1/probe")

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == ("effect_probe_unavailable")


@pytest.mark.integration
async def test_cancel_active_v1_missing_plan_writer_never_returns_ack(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(registered_generation=1)
    del app.state.plan_store
    body = _cancel_body_for(
        team_id="team_1",
        user_id="u_1",
        turn_id="t_v1",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post("/api/v1/turns/t_v1/cancel", json=body)

    assert response.status_code == 503
    assert factory.commit_calls == 1
    assert factory.events == ["commit", "signal"]
    assert orchestrator.signals == [("t_v1", 1)]
    assert app.state.session_store.cancel_receipts == {}


@pytest.mark.integration
async def test_cancel_generation_n_never_signals_same_turn_generation_n_plus_one(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(registered_generation=2)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1"),
        )

    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"
    assert response.json()["durable"] is True
    assert factory.authorize_calls == 1
    assert factory.recovery_calls == 1
    assert factory.record_calls == 0
    assert factory.commit_calls == 1
    assert factory.events == ["commit"]
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_active_v1_returns_durable_ack_when_signal_raises(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        fail_signal=True,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1"),
        )

    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"
    assert response.json()["durable"] is True
    assert factory.recovery_calls == 1
    assert factory.record_calls == 0
    assert factory.events == ["commit"]
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_active_v1_never_signals_before_durable_commit(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        fail_commit=True,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1"),
        )

    assert response.status_code == 503
    assert factory.record_calls == 0
    assert factory.commit_calls == 1
    assert factory.events == ["commit"]
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_active_v1_recovers_when_http_ack_ledger_fails(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=2,
        fail_receipt=True,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1"),
        )

    assert response.status_code == 503
    assert factory.commit_calls == 1
    assert factory.recovery_calls == 1
    assert factory.events == ["commit"]
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_paused_v1_commits_control_and_second_click_is_noop(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        active=False,
        terminal_status=TerminalStatus.CLARIFICATION_PENDING,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        body = _cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1")
        first = await c.post("/api/v1/turns/t_v1/cancel", json=body)
        second = await c.post("/api/v1/turns/t_v1/cancel", json=body)

    assert first.status_code == 202
    assert first.json()["status"] == "cancelled"
    assert second.status_code == 202
    assert second.json()["status"] == "cancelled"
    assert second.json()["ack"] == "accepted"
    assert factory.record_calls == 1
    assert factory.commit_calls == 1
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_completed_v1_is_noop_without_new_control(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        active=False,
        terminal_status=TerminalStatus.COMPLETED,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1"),
        )

    assert response.status_code == 202
    assert response.json()["status"] == "noop"
    assert factory.record_calls == 0
    assert factory.commit_calls == 0
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_future_protocol_fails_before_any_signal(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        future=True,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1"),
        )

    assert response.status_code == 409
    assert factory.authorize_calls == 0
    assert factory.record_calls == 0
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_v1_team_admin_cannot_impersonate_session_owner(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_admin", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_admin", turn_id="t_v1"),
        )

    assert response.status_code == 404
    assert app.state.terminal_protocol_store.scope_reads == 0
    assert factory.authorize_calls == 0
    assert orchestrator.signals == []


@pytest.mark.integration
async def test_cancel_cross_user_future_protocol_is_opaque_and_read_free(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_attacker", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        future=True,
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_attacker", turn_id="t_v1"),
        )

    assert response.status_code == 404
    assert app.state.terminal_protocol_store.scope_reads == 0
    assert factory.authorize_calls == 0
    assert orchestrator.signals == []


class _RevokedV1UserStore(_V1UserStore):
    async def get_member_role(self, workspace_id: str, user_id: str):
        assert (workspace_id, user_id) == ("ws_1", "u_1")
        return


@pytest.mark.integration
async def test_cancel_v1_rechecks_current_workspace_membership(
    monkeypatch,
) -> None:
    async def allow(*_args, **_kwargs):
        return SimpleNamespace(id="u_1", team_id="team_1")

    monkeypatch.setattr(turns_routes, "_require_turn_access", allow)
    app, factory, orchestrator = _v1_cancel_app(
        registered_generation=1,
        user_store=_RevokedV1UserStore(),
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            "/api/v1/turns/t_v1/cancel",
            json=_cancel_body_for(team_id="team_1", user_id="u_1", turn_id="t_v1"),
        )

    assert response.status_code == 403
    assert factory.authorize_calls == 0
    assert factory.record_calls == 0
    assert orchestrator.signals == []
