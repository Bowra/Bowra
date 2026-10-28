"""Track D — authed approve via chat/send (plan_execution_id). Pins the
billing/tenancy invariants the CTO review verified by inspection:
  - flag OFF → plan_execution_id ignored (normal send; never reaches plan guard)
  - cross-tenant / unknown plan → 404 (ownership in get_plan WHERE)
  - owned PROPOSED plan → spawns with tr.plan_execution_id set
  - concurrent double-approve → second gets 409 (atomic try_claim_running)

Harness mirrors test_chat_datarc_decouple.py: create_app + fakes + monkeypatched
_run_turn_and_cleanup (capture the TurnRequest without actually running it).
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx2
import pytest
from bowhead_core.events import EventBusRegistry
from bowhead_core.plan_store import PlanStatus, PlanStore
from bowhead_gateway.main import create_app
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.routes.plans import _run_with_plan_claim_heartbeat
from bowhead_gateway.settings import Settings
from fastapi import HTTPException

from bowhead_core import TurnRequest, get_event_registry


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; since 9c6fe1654 every chat worker turn builds the
    physical-usage receipt cipher from it. Without .env in the checkout nothing else provides it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


class _CompletedResult(str):
    def __new__(cls, value: str = "complete") -> _CompletedResult:
        result = super().__new__(cls, value)
        result.status = SimpleNamespace(value="completed")
        result.tokens_input = 0
        result.tokens_output = 0
        result.final_answer = value
        return result


# Real SessionStore objects always expose ``db_path``; the chat worker hands it to the
# physical-usage receipt observer (cfd302117, default on since 9c6fe1654). The observer only
# opens it when a physical model call starts, and this double's run_turn makes none.
_DOUBLE_SESSION_DB_PATH = "/nonexistent/test-session-store.db"


class _FakeSessionStore:
    db_path = _DOUBLE_SESSION_DB_PATH

    def __init__(self) -> None:
        self.released_submission_claims: list[tuple[str, int]] = []
        self.claim_count = 0
        self.failed_turns: list[str] = []
        self.prepared_spawn_failures: list[tuple[str, str]] = []
        self.spawn_failure_outcome = "ready"
        self.mark_failed_raises = False
        self.mark_failed_transitioned = True
        self.worker_terminalized_dispatches: list[str] = []
        self.recovery_settled_dispatches: list[str] = []
        self.dispatch_states: dict[str, str] = {}
        self.turns: dict[str, SimpleNamespace] = {}

    async def get_active_turn(self, session_id: str) -> str | None:
        return None

    async def ensure_session(self, *, session_id: str, workspace_id: str, user_id: str) -> None:
        return None

    async def record_turn(self, *, turn_id: str, session_id: str, user_text: str, user_source=None) -> None:
        self.turns.setdefault(
            turn_id,
            SimpleNamespace(
                id=turn_id,
                session_id=session_id,
                user_text=user_text,
                status="running",
                error_kind=None,
            ),
        )

    async def get_turn(self, turn_id: str) -> SimpleNamespace | None:
        return self.turns.get(turn_id)

    async def get_session(self, _session_id: str) -> None:
        return None

    async def heartbeat_chat_spawn_dispatch(self, **_kwargs: Any) -> str:
        return "renewed"

    async def register_chat_auth_session_generation(self, **_kwargs: Any) -> bool:
        return True

    async def is_chat_auth_session_generation_active(self, **_kwargs: Any) -> bool:
        return True

    async def get_chat_submission_receipt(self, **_kwargs: Any) -> None:
        return None

    async def claim_chat_submission(self, **_kwargs: Any) -> Any:
        self.claim_count += 1
        return type(
            "Claim",
            (),
            {"kind": "claimed", "lease_owner": "lease_plan", "lease_version": 7},
        )()

    async def release_chat_submission_claim(
        self,
        *,
        lease_owner: str,
        lease_version: int,
        **_kwargs: Any,
    ) -> bool:
        self.released_submission_claims.append((lease_owner, lease_version))
        return True

    async def finalize_started_chat_submission(self, **_kwargs: Any) -> None:
        return None

    async def finalize_rejected_chat_submission(self, **_kwargs: Any) -> None:
        return None

    async def finalize_steer_chat_submission(self, **_kwargs: Any) -> None:
        return None

    async def mark_failed(self, *, turn_id: str, **_kwargs: Any) -> bool:
        if self.mark_failed_raises:
            raise RuntimeError("turn store unavailable")
        if self.mark_failed_transitioned:
            self.failed_turns.append(turn_id)
            turn = self.turns.get(turn_id)
            if turn is not None:
                turn.status = "failed"
                turn.error_kind = _kwargs.get("error_kind")
        return self.mark_failed_transitioned

    async def mark_chat_spawn_dispatch_terminal(
        self,
        *,
        turn_id: str,
        **_kwargs: Any,
    ) -> bool:
        self.worker_terminalized_dispatches.append(turn_id)
        self.dispatch_states[turn_id] = "settled"
        return True

    async def prepare_chat_spawn_failure(
        self,
        *,
        turn_id: str,
        session_id: str,
        expected_owner_id: str | None = None,
    ) -> str:
        self.prepared_spawn_failures.append((turn_id, session_id))
        turn = self.turns.get(turn_id)
        if turn is None or turn.status != "running":
            return "untracked"
        if self.spawn_failure_outcome == "drain_pending":
            self.dispatch_states[turn_id] = "drain_pending"
        return self.spawn_failure_outcome

    async def mark_chat_spawn_dispatch_drain_pending(
        self,
        *,
        turn_id: str,
        **_kwargs: Any,
    ) -> bool:
        self.dispatch_states[turn_id] = "drain_pending"
        return True

    async def recover_expired_chat_spawn_dispatches(self) -> list[str]:
        recovered = [turn_id for turn_id, state in self.dispatch_states.items() if state == "drain_pending"]
        for turn_id in recovered:
            self.dispatch_states[turn_id] = "settled"
        self.recovery_settled_dispatches.extend(recovered)
        return recovered

    async def get_chat_spawn_dispatch_projection_state(
        self,
        *,
        turn_id: str,
    ) -> str:
        return self.dispatch_states.get(turn_id, "untracked")

    async def materialize_steer_follow_up(self, **_kwargs: Any) -> list[str]:
        return []

    async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs: Any) -> str:
        turn_id = _kwargs["turn_id"]
        if turn_id not in self.turns:
            return "untracked"
        self.dispatch_states[turn_id] = "active"
        return "claimed"


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.session_store = _FakeSessionStore()
        self.event_registry = EventBusRegistry()
        self.dispatch_owner_id = "test_chat_plan_approve"
        self._cancel_events: dict[str, asyncio.Event] = {}


class _FakePlan:
    def __init__(self, status: str) -> None:
        self.status = status
        self.session_id = "s_plan"
        self.current_rev = 1


class _FakePlanStore:
    """get_plan returns the plan ONLY for the owning dev user (tenancy in WHERE)."""

    def __init__(self, plan: _FakePlan | None, claim: bool = True) -> None:
        self._plan = plan
        self._claim = claim
        self.claimed: list[str] = []
        self.reverted: list[str] = []
        self.bound_turns: list[tuple[str, str]] = []
        self.revalidated_turns: list[tuple[str, str]] = []
        self.released_execution_claims: list[tuple[str, str]] = []

    async def get_plan(self, plan_id: str, *, user_id: str, workspace_id: str) -> _FakePlan | None:
        if self._plan is None or user_id != "u_dev_alice":
            return None
        return self._plan

    async def try_claim_running(self, plan_id: str, turn_id: str, *, expected_rev: int | None = None) -> bool:
        self.claimed.append(plan_id)
        if self._claim:
            self.bound_turns.append((plan_id, turn_id))
        return self._claim

    async def update_plan_status(self, plan_id: str, status: str) -> bool:
        self.reverted.append(status)
        return True

    async def bind_execution_turn(self, plan_id: str, turn_id: str) -> bool:
        self.revalidated_turns.append((plan_id, turn_id))
        return True

    async def release_execution_claim(self, plan_id: str, turn_id: str) -> bool:
        self.released_execution_claims.append((plan_id, turn_id))
        return True


def _app(plan_store: _FakePlanStore | None) -> Any:
    app = create_app()
    app.state.settings = Settings()
    app.state.orchestrator = _FakeOrchestrator()
    app.state.member_resolver = None
    if plan_store is not None:
        app.state.plan_store = plan_store
    return app


async def _post(app: Any, body: dict[str, Any]) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.post("/api/v1/chat/send", json=body)


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    captured: list[Any] = []

    async def _cap(_orch: Any, req: Any, **_kw: Any) -> None:
        captured.append(req)

    monkeypatch.setattr(chat_routes, "_run_turn_and_cleanup", _cap)
    return captured


@pytest.mark.asyncio
async def test_flag_off_ignores_plan_execution_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # flag OFF → _plan_exec_id == "" → normal send; plan guard never runs, id ignored.
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")  # flag now defaults ON; "0" disables
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED))
    app = _app(store)
    captured = _capture(monkeypatch)
    resp = await _post(app, {"workspace_id": "1", "message": "hi", "plan_execution_id": "plan_x"})
    assert resp.status_code == 200
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.01)
    assert captured and captured[0].plan_execution_id is None  # ignored when flag off
    assert store.claimed == []  # plan store never touched
    await get_event_registry().close(captured[0].turn_id)


@pytest.mark.asyncio
async def test_flag_off_strips_explicit_planning_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    app = _app(None)
    captured = _capture(monkeypatch)

    resp = await _post(
        app,
        {"workspace_id": "1", "message": "hi", "planning_mode": "planning"},
    )

    assert resp.status_code == 200
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.01)
    # Planning OFF strips explicit mode and records effective off (not None=Auto),
    # so persisted intent proves this turn creates no automatic plan; replay matches the original.
    assert captured and captured[0].planning_mode == "off"
    await get_event_registry().close(captured[0].turn_id)


@pytest.mark.asyncio
async def test_attachment_only_request_reaches_turn_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    app = _app(None)
    captured = _capture(monkeypatch)

    resp = await _post(
        app,
        {"workspace_id": "1", "message": "", "attachments": ["art_evidence"]},
    )

    assert resp.status_code == 200
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.01)
    assert captured and captured[0].text == ""
    assert captured[0].attachments == ["art_evidence"]
    await get_event_registry().close(captured[0].turn_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control,preference,objective,title,locked,expected",
    [
        ("", "follow_input", "比较两个区域的盈利能力。", "Profit analysis", False, "zh-CN"),
        ("Run the approved plan.", "auto", "比较两个区域的盈利能力。", "Profit analysis", False, "zh-CN"),
        ("", "en-US", "比较两个区域的盈利能力。", "Profit analysis", False, "en-US"),
        ("", "follow_input", "比较两个区域的盈利能力。", "Profit analysis", True, "fr-FR"),
        ("", "follow_input", "", "比较两个区域的盈利能力。", False, "zh-CN"),
        ("", "follow_input", "", "", False, "en-US"),
    ],
)
async def test_plan_confirmation_resolves_language_from_owned_task(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
    preference: str,
    objective: str,
    title: str,
    locked: bool,
    expected: str,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    monkeypatch.delenv("BOWHEAD_I18N_V1_KILL", raising=False)
    plan = _FakePlan(PlanStatus.PROPOSED)
    plan.objective = objective
    plan.title = title
    app = _app(_FakePlanStore(plan=plan))
    app.state.user_settings_store = SimpleNamespace(list_for_user=AsyncMock(return_value={"ui_language": "en-US", "output_language": preference}))
    policy = {"version": 1, "defaults": {"output_language": "fr-FR"}, "locks": {"output_language": True}} if locked else None
    monkeypatch.setattr(chat_routes, "_load_team_language_policy", AsyncMock(return_value=policy))
    fallback = AsyncMock(return_value=None)
    monkeypatch.setattr(chat_routes, "_resolve_session_last_output_language", fallback)
    captured = _capture(monkeypatch)
    response = await _post(app, {"workspace_id": "1", "message": control, "plan_execution_id": "plan_language"})
    assert response.status_code == 200, response.text
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.01)
    assert captured
    request = captured[0]
    await get_event_registry().close(request.turn_id)
    assert request.language_context.ui_language == "en-US"
    assert request.language_context.output_language == expected
    assert request.session_id == plan.session_id
    assert request.text == (control or "(执行已批准的计划)")
    if not objective and not title:
        assert fallback.call_args.args[1] == plan.session_id
    else:
        fallback.assert_not_called()


@pytest.mark.asyncio
async def test_cross_tenant_plan_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=None)  # get_plan → None (not owner / unknown)
    app = _app(store)
    _capture(monkeypatch)
    resp = await _post(app, {"workspace_id": "1", "plan_execution_id": "plan_other", "message": ""})
    assert resp.status_code == 404
    assert store.claimed == []  # never claimed a plan we don't own


@pytest.mark.asyncio
async def test_transient_plan_store_outage_never_claims_submission_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("="),
    )
    app = _app(None)
    _capture(monkeypatch)

    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_transient",
            "message": "",
            "client_submission_id": "d89bb81a-c946-4cc6-a74f-26f0c80f331e",
            "submission_kind": "start",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "planning_unavailable"
    assert app.state.orchestrator.session_store.claim_count == 0
    assert app.state.orchestrator.session_store.released_submission_claims == []


@pytest.mark.asyncio
async def test_plan_datarc_gate_failure_never_claims_submission_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"d" * 32).decode().rstrip("="),
    )
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED))
    app = _app(store)

    async def fail_datarc(*_args: Any, **_kwargs: Any) -> None:
        raise HTTPException(status_code=503, detail={"error": {"code": "datarc_unavailable"}})

    monkeypatch.setattr(chat_routes, "resolve_datarc_turn_context", fail_datarc)
    payload = {
        "workspace_id": "1",
        "plan_execution_id": "plan_datarc",
        "message": "",
        "client_submission_id": "d89bb81a-c946-4cc6-a74f-26f0c80f331f",
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
    }

    first = await _post(app, payload)
    retry = await _post(app, payload)

    assert first.status_code == retry.status_code == 503
    assert app.state.orchestrator.session_store.claim_count == 0
    assert store.claimed == []


class _CreditExhaustedUserStore:
    async def get_member_role(self, *_args: Any) -> str:
        return "member"

    async def get_user(self, _user_id: str) -> Any:
        return SimpleNamespace(credit_balance=0, credit_quota=100)

    async def get_team(self, _team_id: str) -> Any:
        return SimpleNamespace(team_credit_balance=100, team_credit_quota=100)

    async def get_team_llm_config_with_version(self, _team_id: str) -> tuple[dict[str, Any], int]:
        return {}, 0

    async def record_credit_exhaustion_event(self, **_kwargs: Any) -> dict[str, Any]:
        return {"id": "credit_event"}


@pytest.mark.asyncio
async def test_plan_credit_gate_failure_never_claims_submission_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("="),
    )

    async def authenticated_identity(_request: Any) -> tuple[str, str]:
        return "u_dev_alice", "team_credit"

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", authenticated_identity)
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED))
    app = _app(store)
    app.state.user_store = _CreditExhaustedUserStore()
    payload = {
        "workspace_id": "1",
        "plan_execution_id": "plan_credit",
        "message": "",
        "client_submission_id": "d89bb81a-c946-4cc6-a74f-26f0c80f3320",
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
    }

    first = await _post(app, payload)
    retry = await _post(app, payload)

    assert first.status_code == retry.status_code == 402
    assert app.state.orchestrator.session_store.claim_count == 0
    assert store.claimed == []


@pytest.mark.asyncio
async def test_owned_proposed_spawns_with_plan_execution_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED), claim=True)
    app = _app(store)
    captured = _capture(monkeypatch)
    resp = await _post(app, {"workspace_id": "1", "plan_execution_id": "plan_mine", "message": ""})
    assert resp.status_code == 200
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.01)
    assert captured and captured[0].plan_execution_id == "plan_mine"
    assert store.claimed == ["plan_mine"]
    await get_event_registry().close(captured[0].turn_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_during_admission", [False, True])
@pytest.mark.parametrize("browser_revision", [None, 1, 2])
async def test_approval_claim_uses_plan_revision_read_before_model_setup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    edit_during_admission: bool,
    browser_revision: int | None,
) -> None:
    from bowhead_core.plan_store import new_step

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = PlanStore(str(tmp_path / "approval-revision.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s_plan", workspace_id="1", user_id="u_dev_alice", title="reviewed plan")
    await store.add_steps([new_step(plan_id=plan.id, seq=0, agent_name="compute")])
    assert await store.replace_steps(
        plan.id, [new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="other tab edit")], expected_rev=plan.current_rev
    )
    plan = await store.get_plan(plan.id, user_id="u_dev_alice", workspace_id="1")
    app = _app(store)
    captured = _capture(monkeypatch)
    original_resolver = chat_routes._resolve_web_language_inputs

    async def resolve_with_concurrent_edit(*args: Any, **kwargs: Any) -> Any:
        result = await original_resolver(*args, **kwargs)
        if edit_during_admission:
            assert await store.replace_steps(
                plan.id,
                [new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="edited")],
                expected_rev=plan.current_rev,
            )
        return result

    monkeypatch.setattr(chat_routes, "_resolve_web_language_inputs", resolve_with_concurrent_edit)
    response = await _post(app, {"workspace_id": "1", "plan_execution_id": plan.id, "message": "", "expected_plan_revision": browser_revision})
    saved = await store.get_plan(plan.id, user_id="u_dev_alice", workspace_id="1")
    if edit_during_admission or browser_revision == 1:
        assert response.status_code == 409
        assert response.json()["detail"]["error"]["code"] == "plan_not_approvable"
        assert saved.status == PlanStatus.PROPOSED
        assert saved.current_rev == plan.current_rev + int(edit_during_admission)
        assert saved.execution_turn_id is None
        assert not captured
        assert not app.state.orchestrator.session_store.turns
    else:
        assert response.status_code == 200
        for _ in range(50):
            if captured:
                break
            await asyncio.sleep(0.01)
        assert captured and captured[0].plan_execution_id == plan.id
        assert captured[0].expected_plan_revision == browser_revision
        assert saved.current_rev == plan.current_rev
        await get_event_registry().close(captured[0].turn_id)


@pytest.mark.asyncio
async def test_post_admission_plan_fence_blocks_stale_legacy_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LostClaimStore(_FakePlanStore):
        async def bind_execution_turn(self, plan_id: str, turn_id: str) -> bool:
            self.revalidated_turns.append((plan_id, turn_id))
            return False

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _LostClaimStore(plan=_FakePlan(PlanStatus.PROPOSED), claim=True)
    app = _app(store)
    captured = _capture(monkeypatch)

    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_stale_owner",
            "message": "",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == ("plan_execution_claim_lost")
    [(plan_id, turn_id)] = store.bound_turns
    assert store.revalidated_turns == [(plan_id, turn_id)]
    assert captured == []
    producer, _floor = await app.state.orchestrator.event_registry.projection_checkpoint(
        turn_id,
    )
    assert producer is None


@pytest.mark.asyncio
async def test_preworker_plan_fence_blocks_owner_lost_after_turn_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LostAfterPersistStore(_FakePlanStore):
        revalidation_count = 0

        async def bind_execution_turn(self, plan_id: str, turn_id: str) -> bool:
            self.revalidation_count += 1
            self.revalidated_turns.append((plan_id, turn_id))
            return self.revalidation_count == 1

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _LostAfterPersistStore(
        plan=_FakePlan(PlanStatus.PROPOSED),
        claim=True,
    )
    app = _app(store)
    captured = _capture(monkeypatch)

    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_lost_after_persist",
            "message": "",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == ("plan_execution_claim_lost")
    [(plan_id, turn_id)] = store.bound_turns
    assert store.revalidated_turns == [(plan_id, turn_id), (plan_id, turn_id)]
    assert captured == []
    assert app.state.orchestrator.session_store.failed_turns == [turn_id]
    assert app.state.orchestrator.session_store.recovery_settled_dispatches == [
        turn_id,
    ]


@pytest.mark.asyncio
async def test_plan_spawn_failure_releases_bound_execution_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED), claim=True)
    app = _app(store)

    async def fail_spawn(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("worker scheduler unavailable")

    persisted_events = []

    async def persist_event(event: Any) -> bool:
        persisted_events.append(event)
        return True

    registry = EventBusRegistry(default_log_sink=persist_event)
    app.state.orchestrator.event_registry = registry
    monkeypatch.setattr(chat_routes, "get_event_registry", lambda: registry)

    import bowhead_gateway.turn_service as turn_service

    monkeypatch.setattr(turn_service, "spawn_turn", fail_spawn)
    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_spawn_failure",
            "message": "",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "turn_dispatch_unavailable"
    [(plan_id, turn_id)] = store.bound_turns
    assert plan_id == "plan_spawn_failure"
    assert store.released_execution_claims == [(plan_id, turn_id)]
    assert app.state.orchestrator.session_store.failed_turns == [turn_id]
    assert app.state.orchestrator.session_store.prepared_spawn_failures == [
        (turn_id, "s_plan"),
    ]
    assert app.state.orchestrator.session_store.worker_terminalized_dispatches == []
    assert app.state.orchestrator.session_store.recovery_settled_dispatches == [turn_id]
    # Recovery writes the canonical tail in the Store transaction; the retired
    # process-local bus sink is deliberately not another terminal writer.
    assert persisted_events == []


@pytest.mark.asyncio
async def test_persist_commit_outcome_unknown_recovers_turn_before_reraise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    plan_store = _FakePlanStore(
        plan=_FakePlan(PlanStatus.PROPOSED),
        claim=True,
    )
    app = _app(plan_store)
    registry = EventBusRegistry()
    app.state.orchestrator.event_registry = registry
    real_persist = chat_routes._persist_turn_row_for_stream

    async def commit_then_cancel(*args: Any, **kwargs: Any) -> None:
        await real_persist(*args, **kwargs)
        raise asyncio.CancelledError("commit acknowledgement was lost")

    monkeypatch.setattr(
        chat_routes,
        "_persist_turn_row_for_stream",
        commit_then_cancel,
    )

    with pytest.raises(
        asyncio.CancelledError,
        match="commit acknowledgement was lost",
    ):
        await _post(
            app,
            {
                "workspace_id": "1",
                "plan_execution_id": "plan_commit_unknown",
                "message": "",
            },
        )

    [(plan_id, turn_id)] = plan_store.released_execution_claims
    assert plan_id == "plan_commit_unknown"
    session_store = app.state.orchestrator.session_store
    assert session_store.failed_turns == [turn_id]
    assert session_store.prepared_spawn_failures == [(turn_id, "s_plan")]
    assert session_store.recovery_settled_dispatches == [turn_id]
    producer, _floor = await registry.projection_checkpoint(turn_id)
    assert producer is None


@pytest.mark.asyncio
async def test_spawn_cleanup_survives_repeated_cancel_through_plan_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    class BlockingReleasePlanStore(_FakePlanStore):
        async def release_execution_claim(
            self,
            plan_id: str,
            turn_id: str,
        ) -> bool:
            release_started.set()
            await allow_release.wait()
            return await super().release_execution_claim(plan_id, turn_id)

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    plan_store = BlockingReleasePlanStore(
        plan=_FakePlan(PlanStatus.PROPOSED),
        claim=True,
    )
    app = _app(plan_store)
    registry = EventBusRegistry()
    app.state.orchestrator.event_registry = registry

    import bowhead_gateway.turn_service as turn_service

    async def cancel_spawn(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError("cancel during spawn")

    monkeypatch.setattr(turn_service, "spawn_turn", cancel_spawn)
    send_task = asyncio.create_task(
        _post(
            app,
            {
                "workspace_id": "1",
                "plan_execution_id": "plan_repeated_cancel",
                "message": "",
            },
        ),
    )
    await asyncio.wait_for(release_started.wait(), timeout=1)
    [(plan_id, turn_id)] = plan_store.bound_turns
    producer, _floor = await registry.projection_checkpoint(turn_id)
    assert producer is None

    send_task.cancel("second cancel")
    await asyncio.sleep(0)
    send_task.cancel("third cancel")
    await asyncio.sleep(0)
    assert not send_task.done()
    allow_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(send_task, timeout=1)
    assert plan_store.released_execution_claims == [(plan_id, turn_id)]
    session_store = app.state.orchestrator.session_store
    assert session_store.recovery_settled_dispatches == [turn_id]


@pytest.mark.asyncio
async def test_uncertain_spawn_failure_returns_receipt_for_foreign_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    plan_store = _FakePlanStore(
        plan=_FakePlan(PlanStatus.PROPOSED),
        claim=True,
    )
    app = _app(plan_store)
    app.state.orchestrator.session_store.spawn_failure_outcome = "foreign_active"

    import bowhead_gateway.turn_service as turn_service

    async def lose_owner_race(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("second dispatch claim database outage")

    monkeypatch.setattr(turn_service, "spawn_turn", lose_owner_race)
    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_owner_loser",
            "message": "",
        },
    )

    assert response.status_code == 200
    [(plan_id, turn_id)] = plan_store.bound_turns
    assert plan_id == "plan_owner_loser"
    assert plan_store.released_execution_claims == [(plan_id, turn_id)]
    session_store = app.state.orchestrator.session_store
    assert session_store.failed_turns == []
    assert session_store.prepared_spawn_failures == [(turn_id, "s_plan")]
    assert session_store.worker_terminalized_dispatches == []
    assert session_store.recovery_settled_dispatches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["persist", "spawn"])
async def test_send_cancellation_releases_plan_claim_and_exact_bus(
    monkeypatch: pytest.MonkeyPatch,
    cancel_phase: str,
) -> None:
    class TrackingRegistry(EventBusRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.created: list[tuple[str, object]] = []
            self.close_calls: list[tuple[str, object | None]] = []

        async def get_or_create(self, turn_id: str, **kwargs: Any):
            bus = await super().get_or_create(turn_id, **kwargs)
            self.created.append((turn_id, bus))
            return bus

        async def close(self, turn_id: str, *, expected=None) -> None:
            self.close_calls.append((turn_id, expected))
            await super().close(turn_id, expected=expected)

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    plan_store = _FakePlanStore(
        plan=_FakePlan(PlanStatus.PROPOSED),
        claim=True,
    )
    app = _app(plan_store)
    registry = TrackingRegistry()
    app.state.orchestrator.event_registry = registry

    if cancel_phase == "persist":

        async def cancel_persist(*_args: Any, **_kwargs: Any) -> None:
            raise asyncio.CancelledError("cancel during persist")

        monkeypatch.setattr(
            chat_routes,
            "_persist_turn_row_for_stream",
            cancel_persist,
        )
    else:
        import bowhead_gateway.turn_service as turn_service

        async def cancel_spawn(*_args: Any, **_kwargs: Any) -> None:
            raise asyncio.CancelledError("cancel during spawn")

        monkeypatch.setattr(turn_service, "spawn_turn", cancel_spawn)

    with pytest.raises(asyncio.CancelledError):
        await _post(
            app,
            {
                "workspace_id": "1",
                "plan_execution_id": f"plan_cancel_{cancel_phase}",
                "message": "",
            },
        )

    [(turn_id, admitted_bus)] = registry.created
    assert registry.close_calls == [(turn_id, admitted_bus)]
    assert plan_store.released_execution_claims == [
        (f"plan_cancel_{cancel_phase}", turn_id),
    ]
    session_store = app.state.orchestrator.session_store
    assert session_store.failed_turns == ([] if cancel_phase == "persist" else [turn_id])
    assert session_store.recovery_settled_dispatches == ([] if cancel_phase == "persist" else [turn_id])


@pytest.mark.asyncio
@pytest.mark.parametrize("sink_outcome", ["rejected", "exception"])
async def test_spawn_failure_recovery_does_not_depend_on_retired_bus_sink(
    monkeypatch: pytest.MonkeyPatch,
    sink_outcome: str,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED), claim=True)
    app = _app(store)
    session_store = app.state.orchestrator.session_store

    async def reject_event(_event: Any) -> bool:
        if sink_outcome == "exception":
            raise RuntimeError("durable sink unavailable")
        return False

    registry = EventBusRegistry(default_log_sink=reject_event)
    captured_buses = []
    original_get_or_create = registry.get_or_create

    async def capture_bus(turn_id: str, **kwargs: Any):
        bus = await original_get_or_create(turn_id, **kwargs)
        captured_buses.append(bus)
        return bus

    monkeypatch.setattr(registry, "get_or_create", capture_bus)
    app.state.orchestrator.event_registry = registry
    monkeypatch.setattr(chat_routes, "get_event_registry", lambda: registry)

    async def fail_spawn(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("worker scheduler unavailable")

    import bowhead_gateway.turn_service as turn_service

    monkeypatch.setattr(turn_service, "spawn_turn", fail_spawn)
    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_spawn_failure_persist_rejected",
            "message": "",
        },
    )

    assert response.status_code == 503
    assert len(session_store.prepared_spawn_failures) == 1
    [(turn_id, _session_id)] = session_store.prepared_spawn_failures
    assert session_store.worker_terminalized_dispatches == []
    assert session_store.recovery_settled_dispatches == [turn_id]
    assert captured_buses
    assert all(event.event_type not in {"done", "error"} for bus in captured_buses for event in bus.snapshot())
    producer_bus, _floor = await registry.projection_checkpoint(turn_id)
    assert producer_bus is None


@pytest.mark.asyncio
async def test_spawn_failure_recovery_does_not_depend_on_retired_bus_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED), claim=True)
    app = _app(store)
    session_store = app.state.orchestrator.session_store

    class _UndrainableSink:
        async def __call__(self, _event: Any) -> bool:
            return True

        async def drain_turn(self, _turn_id: str) -> None:
            raise RuntimeError("ordered tail unavailable")

    registry = EventBusRegistry(default_log_sink=_UndrainableSink())
    app.state.orchestrator.event_registry = registry
    monkeypatch.setattr(chat_routes, "get_event_registry", lambda: registry)

    async def fail_spawn(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("worker scheduler unavailable")

    import bowhead_gateway.turn_service as turn_service

    monkeypatch.setattr(turn_service, "spawn_turn", fail_spawn)
    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_spawn_failure_drain_failed",
            "message": "",
        },
    )

    assert response.status_code == 503
    assert len(session_store.prepared_spawn_failures) == 1
    [(turn_id, _session_id)] = session_store.prepared_spawn_failures
    assert session_store.worker_terminalized_dispatches == []
    assert session_store.recovery_settled_dispatches == [turn_id]
    producer_bus, _floor = await registry.projection_checkpoint(turn_id)
    assert producer_bus is None


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_outcome", ("exception", "concurrent_terminal"))
async def test_spawn_failure_unconfirmed_turn_marker_retires_bus_without_wire_terminal(
    monkeypatch: pytest.MonkeyPatch,
    marker_outcome: str,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED), claim=True)
    app = _app(store)
    session_store = app.state.orchestrator.session_store
    session_store.mark_failed_raises = marker_outcome == "exception"
    session_store.mark_failed_transitioned = marker_outcome != "concurrent_terminal"

    class _CollectingSink:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def __call__(self, event: Any) -> bool:
            self.events.append(event)
            return True

        async def drain_turn(self, _turn_id: str) -> None:
            return None

    sink = _CollectingSink()
    registry = EventBusRegistry(default_log_sink=sink)
    captured_buses = []
    original_get_or_create = registry.get_or_create

    async def capture_bus(turn_id: str, **kwargs: Any):
        bus = await original_get_or_create(turn_id, **kwargs)
        captured_buses.append(bus)
        return bus

    monkeypatch.setattr(registry, "get_or_create", capture_bus)
    app.state.orchestrator.event_registry = registry
    monkeypatch.setattr(chat_routes, "get_event_registry", lambda: registry)

    async def fail_spawn(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("worker scheduler unavailable")

    import bowhead_gateway.turn_service as turn_service

    monkeypatch.setattr(turn_service, "spawn_turn", fail_spawn)
    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_spawn_failure_turn_store_outage",
            "message": "",
        },
    )

    assert response.status_code == 503
    [(plan_id, turn_id)] = store.bound_turns
    assert store.released_execution_claims == [(plan_id, turn_id)]
    assert session_store.failed_turns == []
    assert session_store.prepared_spawn_failures == [(turn_id, "s_plan")]
    assert session_store.worker_terminalized_dispatches == []
    assert session_store.recovery_settled_dispatches == []
    assert all(event.event_type not in {"done", "error"} for event in sink.events)
    assert captured_buses
    assert all(event.event_type not in {"done", "error"} for bus in captured_buses for event in bus.snapshot())
    producer_bus, _floor = await registry.projection_checkpoint(turn_id)
    assert producer_bus is None


@pytest.mark.asyncio
async def test_spawn_failure_with_accepted_steer_suppresses_parent_wire_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED), claim=True)
    app = _app(store)
    session_store = app.state.orchestrator.session_store
    session_store.spawn_failure_outcome = "drain_pending"
    registry = EventBusRegistry()
    captured_buses = []
    original_get_or_create = registry.get_or_create

    async def capture_bus(turn_id: str, **kwargs: Any):
        bus = await original_get_or_create(turn_id, **kwargs)
        captured_buses.append(bus)
        return bus

    monkeypatch.setattr(registry, "get_or_create", capture_bus)
    app.state.orchestrator.event_registry = registry
    monkeypatch.setattr(chat_routes, "get_event_registry", lambda: registry)

    async def fail_spawn(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("worker scheduler unavailable after steer")

    import bowhead_gateway.turn_service as turn_service

    monkeypatch.setattr(turn_service, "spawn_turn", fail_spawn)
    response = await _post(
        app,
        {
            "workspace_id": "1",
            "plan_execution_id": "plan_spawn_failure_with_steer",
            "message": "",
        },
    )

    assert response.status_code == 503
    assert len(session_store.prepared_spawn_failures) == 1
    [(turn_id, session_id)] = session_store.prepared_spawn_failures
    assert session_id == "s_plan"
    assert session_store.worker_terminalized_dispatches == []
    assert session_store.recovery_settled_dispatches == [turn_id]
    assert captured_buses
    assert all(event.event_type not in {"done", "error"} for bus in captured_buses for event in bus.snapshot())
    producer_bus, _floor = await registry.projection_checkpoint(turn_id)
    assert producer_bus is None


@pytest.mark.asyncio
async def test_double_approve_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED), claim=False)  # already running
    app = _app(store)
    _capture(monkeypatch)
    resp = await _post(app, {"workspace_id": "1", "plan_execution_id": "plan_mine", "message": ""})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_dev_plan_preworker_failure_releases_only_its_exact_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PlanStore(str(tmp_path / "plan-preworker.db"))
    await store.initialize()
    plan = await store.create_plan(
        session_id="s_dev_preworker",
        workspace_id="ws_dev_preworker",
        user_id="u_dev_preworker",
        title="preworker failure",
    )
    assert await store.try_claim_running(plan.id, "t_dev_preworker_old")

    async def fail_admission(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("admission unavailable")

    import bowhead_gateway.turn_service as turn_service

    monkeypatch.setattr(
        turn_service,
        "admit_legacy_chat_execution",
        fail_admission,
    )
    request = TurnRequest(
        session_id="s_dev_preworker",
        workspace_id="ws_dev_preworker",
        user_id="u_dev_preworker",
        member_uid="m_dev_preworker",
        text="execute",
        turn_id="t_dev_preworker_old",
        plan_execution_id=plan.id,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            orchestrator=SimpleNamespace(event_registry=EventBusRegistry()),
        ),
    )

    with pytest.raises(RuntimeError, match="admission unavailable"):
        await _run_with_plan_claim_heartbeat(app, store, plan.id, request)

    released = await store.get_plan(
        plan.id,
        user_id="u_dev_preworker",
        workspace_id="ws_dev_preworker",
    )
    assert released is not None
    assert released.status == PlanStatus.BLOCKED
    assert released.execution_turn_id is None

    assert await store.try_claim_running(plan.id, "t_dev_preworker_new")
    assert not await store.release_execution_claim(
        plan.id,
        "t_dev_preworker_old",
    )
    reclaimed = await store.get_plan(
        plan.id,
        user_id="u_dev_preworker",
        workspace_id="ws_dev_preworker",
    )
    assert reclaimed is not None
    assert reclaimed.status == PlanStatus.RUNNING
    assert reclaimed.execution_turn_id == "t_dev_preworker_new"


@pytest.mark.asyncio
async def test_dev_plan_heartbeat_cancels_worker_after_turn_claim_is_lost() -> None:
    worker_cancelled = asyncio.Event()

    class _Orchestrator:
        def __init__(self) -> None:
            self.event_registry = EventBusRegistry()
            self.session_store = _FakeSessionStore()
            self.dispatch_owner_id = "test_dev_plan_lost"
            self._cancel_events: dict[str, asyncio.Event] = {}

        async def run_turn(self, _request: TurnRequest) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                worker_cancelled.set()

    class _LostClaimStore:
        renewals = 0
        releases = 0

        async def bind_execution_turn(self, plan_id: str, turn_id: str) -> bool:
            assert plan_id == "plan_dev"
            assert turn_id == "t_plan_dev"
            self.renewals += 1
            return False

        async def heartbeat_execution_tail(self, plan_id: str, turn_id: str) -> bool:
            assert plan_id == "plan_dev"
            assert turn_id == "t_plan_dev"
            return False

        async def release_execution_turn(self, plan_id: str, turn_id: str) -> bool:
            self.releases += 1
            return False

        async def release_execution_claim(self, plan_id: str, turn_id: str) -> bool:
            self.releases += 1
            return False

    store = _LostClaimStore()
    orchestrator = _Orchestrator()
    orchestrator.plan_store = store
    app = SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator))
    request = TurnRequest(
        session_id="s_dev",
        workspace_id="ws_dev",
        user_id="u_dev",
        member_uid="m_dev",
        text="execute",
        turn_id="t_plan_dev",
        plan_execution_id="plan_dev",
    )

    with pytest.raises(RuntimeError, match="plan_execution_claim_lost"):
        await _run_with_plan_claim_heartbeat(
            app,
            store,
            "plan_dev",
            request,
            heartbeat_interval_s=0.001,
        )

    assert store.renewals == 1
    assert store.releases == 0
    assert worker_cancelled.is_set()


@pytest.mark.asyncio
async def test_dev_plan_heartbeat_allows_exact_owner_to_finish_terminal_tail() -> None:
    finish_worker = asyncio.Event()

    class _Orchestrator:
        def __init__(self) -> None:
            self.event_registry = EventBusRegistry()
            self.session_store = _FakeSessionStore()
            self.dispatch_owner_id = "test_dev_plan_terminal"
            self._cancel_events: dict[str, asyncio.Event] = {}

        async def run_turn(self, _request: TurnRequest) -> _CompletedResult:
            await finish_worker.wait()
            self.session_store.turns[_request.turn_id].status = "completed"
            return _CompletedResult("terminal-tail-complete")

    class _TerminalOwnerStore:
        renewals = 0
        tail_heartbeats = 0
        releases = 0

        async def bind_execution_turn(self, plan_id: str, turn_id: str) -> bool:
            assert plan_id == "plan_dev_terminal"
            assert turn_id == "t_plan_dev_terminal"
            self.renewals += 1
            return False

        async def heartbeat_execution_tail(self, plan_id: str, turn_id: str) -> bool:
            assert plan_id == "plan_dev_terminal"
            assert turn_id == "t_plan_dev_terminal"
            self.tail_heartbeats += 1
            finish_worker.set()
            # Return only after the worker finished its terminal tail. With the 1 ms
            # interval, a slow event loop could otherwise start a second heartbeat
            # round before the worker task completes and make the counts flaky.
            worker = next(task for task in asyncio.all_tasks() if task.get_name() == "dev_plan_run:plan_dev_terminal:t_plan_dev_terminal")
            await asyncio.wait({worker})
            return True

        async def release_execution_turn(self, plan_id: str, turn_id: str) -> bool:
            assert plan_id == "plan_dev_terminal"
            assert turn_id == "t_plan_dev_terminal"
            self.releases += 1
            return True

    store = _TerminalOwnerStore()
    orchestrator = _Orchestrator()
    orchestrator.plan_store = store
    app = SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator))
    request = TurnRequest(
        session_id="s_dev_terminal",
        workspace_id="ws_dev_terminal",
        user_id="u_dev_terminal",
        member_uid="m_dev_terminal",
        text="execute",
        turn_id="t_plan_dev_terminal",
        plan_execution_id="plan_dev_terminal",
    )

    result = await _run_with_plan_claim_heartbeat(
        app,
        store,
        "plan_dev_terminal",
        request,
        heartbeat_interval_s=0.001,
    )

    assert result == "terminal-tail-complete"
    assert store.renewals == 1
    assert store.tail_heartbeats == 1
    assert store.releases == 1


@pytest.mark.asyncio
async def test_dev_plan_bus_close_failure_does_not_mask_success() -> None:
    class CloseFailRegistry(EventBusRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls: list[tuple[str, object | None]] = []

        async def close(self, turn_id: str, *, expected=None) -> None:
            self.close_calls.append((turn_id, expected))
            raise RuntimeError("close boom")

    class _Orchestrator:
        def __init__(self) -> None:
            self.event_registry = CloseFailRegistry()
            self.session_store = _FakeSessionStore()
            self.dispatch_owner_id = "test_dev_plan_close"
            self._cancel_events: dict[str, asyncio.Event] = {}

        async def run_turn(self, _request: TurnRequest) -> _CompletedResult:
            self.session_store.turns[_request.turn_id].status = "completed"
            return _CompletedResult()

    class _Store:
        releases = 0

        async def release_execution_turn(
            self,
            plan_id: str,
            turn_id: str,
        ) -> bool:
            assert (plan_id, turn_id) == ("plan_close", "t_plan_close")
            self.releases += 1
            return True

        async def release_execution_claim(
            self,
            plan_id: str,
            turn_id: str,
        ) -> bool:
            self.releases += 1
            return True

    orchestrator = _Orchestrator()
    store = _Store()
    orchestrator.plan_store = store
    request = TurnRequest(
        session_id="s_plan_close",
        workspace_id="ws_plan_close",
        user_id="u_plan_close",
        member_uid="m_plan_close",
        text="execute",
        turn_id="t_plan_close",
        plan_execution_id="plan_close",
    )

    result = await _run_with_plan_claim_heartbeat(
        SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator)),
        store,
        "plan_close",
        request,
        heartbeat_interval_s=0.001,
    )

    assert result == "complete"
    assert store.releases == 1
    assert orchestrator.event_registry.close_calls
    assert all(
        closed_turn_id == "t_plan_close" and expected_bus is not None for closed_turn_id, expected_bus in orchestrator.event_registry.close_calls
    )


@pytest.mark.asyncio
async def test_dev_plan_release_failure_still_exact_closes_bus() -> None:
    class TrackingRegistry(EventBusRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls: list[tuple[str, object | None]] = []

        async def close(self, turn_id: str, *, expected=None) -> None:
            self.close_calls.append((turn_id, expected))
            await super().close(turn_id, expected=expected)

    class _Orchestrator:
        def __init__(self) -> None:
            self.event_registry = TrackingRegistry()
            self.session_store = _FakeSessionStore()
            self.dispatch_owner_id = "test_dev_plan_release"
            self._cancel_events: dict[str, asyncio.Event] = {}

        async def run_turn(self, _request: TurnRequest) -> _CompletedResult:
            self.session_store.turns[_request.turn_id].status = "completed"
            return _CompletedResult()

    class _Store:
        async def bind_execution_turn(
            self,
            _plan_id: str,
            _turn_id: str,
        ) -> bool:
            return False

        async def heartbeat_execution_tail(
            self,
            _plan_id: str,
            _turn_id: str,
        ) -> bool:
            return True

        async def release_execution_turn(
            self,
            _plan_id: str,
            _turn_id: str,
        ) -> bool:
            raise RuntimeError("release unavailable")

        async def release_execution_claim(
            self,
            _plan_id: str,
            _turn_id: str,
        ) -> bool:
            raise RuntimeError("release unavailable")

    orchestrator = _Orchestrator()
    store = _Store()
    orchestrator.plan_store = store
    request = TurnRequest(
        session_id="s_plan_release",
        workspace_id="ws_plan_release",
        user_id="u_plan_release",
        member_uid="m_plan_release",
        text="execute",
        turn_id="t_plan_release",
        plan_execution_id="plan_release",
    )

    result = await _run_with_plan_claim_heartbeat(
        SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator)),
        store,
        "plan_release",
        request,
        heartbeat_interval_s=0.001,
    )

    assert result == "complete"
    assert orchestrator.event_registry.close_calls
    assert all(turn_id == "t_plan_release" and expected_bus is not None for turn_id, expected_bus in orchestrator.event_registry.close_calls)


@pytest.mark.asyncio
async def test_production_plan_worker_releases_retry_fence_after_full_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = asyncio.Event()
    tail_completed = asyncio.Event()

    class _TailSessionStore(_FakeSessionStore):
        async def materialize_steer_follow_up(self, **_kwargs: Any) -> list[str]:
            tail_completed.set()
            return []

    class _PlanStore:
        async def release_execution_turn(self, plan_id: str, turn_id: str) -> bool:
            assert plan_id == "plan_prod_tail"
            assert turn_id == "t_plan_prod_tail"
            assert tail_completed.is_set()
            released.set()
            return True

    class _Orchestrator:
        session_store = _TailSessionStore()
        plan_store = _PlanStore()

        async def run_turn(self, _request: TurnRequest) -> _CompletedResult:
            self.session_store.turns[_request.turn_id] = SimpleNamespace(
                id=_request.turn_id,
                session_id=_request.session_id,
                status="completed",
            )
            return _CompletedResult()

    request = TurnRequest(
        session_id="s_prod_tail",
        workspace_id="ws_prod_tail",
        user_id="u_prod_tail",
        member_uid="m_prod_tail",
        text="execute",
        turn_id="t_plan_prod_tail",
        plan_execution_id="plan_prod_tail",
    )
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _seconds: real_sleep(0))
    orchestrator = _Orchestrator()
    orchestrator.event_registry = EventBusRegistry()
    from bowhead_gateway.turn_service import admit_legacy_chat_execution

    admitted_bus = await admit_legacy_chat_execution(orchestrator, request)

    await chat_routes._run_turn_and_cleanup(
        orchestrator,
        request,
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )

    assert released.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic_auto_plan", [False, True])
async def test_terminal_plan_worker_releases_retry_fence_after_receipt_tail(
    monkeypatch: pytest.MonkeyPatch,
    dynamic_auto_plan: bool,
) -> None:
    released = asyncio.Event()
    retired = asyncio.Event()

    class _PlanStore:
        async def release_execution_turn(self, plan_id: str, turn_id: str) -> bool:
            assert not dynamic_auto_plan
            assert (plan_id, turn_id) == (
                "plan_terminal_tail",
                "t_terminal_plan_tail",
            )
            assert retired.is_set()
            released.set()
            return True

        async def release_execution_turn_for_turn(self, turn_id: str) -> bool:
            assert dynamic_auto_plan
            assert turn_id == "t_terminal_plan_tail"
            assert retired.is_set()
            released.set()
            return True

    class _TerminalRuntime:
        bus = SimpleNamespace(receipt=SimpleNamespace(status="completed"))

        async def start_execution(self) -> None:
            return None

        async def retire(self) -> None:
            retired.set()

    class _Orchestrator:
        plan_store = _PlanStore()

        async def run_turn(self, _request: TurnRequest) -> _CompletedResult:
            return _CompletedResult()

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(chat_routes.asyncio, "sleep", _no_sleep)
    request = TurnRequest(
        session_id="s_terminal_plan_tail",
        workspace_id="ws_terminal_plan_tail",
        user_id="u_terminal_plan_tail",
        member_uid="m_terminal_plan_tail",
        text="execute",
        turn_id="t_terminal_plan_tail",
        plan_execution_id=(None if dynamic_auto_plan else "plan_terminal_tail"),
        terminal_runtime=_TerminalRuntime(),
    )

    await chat_routes._run_turn_and_cleanup(
        _Orchestrator(),
        request,
        cleanup_grace_s=0,
    )

    assert released.is_set()


@pytest.mark.parametrize("revision", [0, -1, True, "1", 1.5])
def test_plan_approval_requires_positive_integer_revision(revision: object) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        chat_routes.ChatSendRequest(workspace_id="1", plan_execution_id="plan_1", expected_plan_revision=revision)


def test_plan_revision_requires_an_execution_identity() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        chat_routes.ChatSendRequest(workspace_id="1", expected_plan_revision=1)


@pytest.mark.asyncio
async def test_versioned_plan_approval_does_not_become_normal_send_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = _FakePlanStore(plan=_FakePlan(PlanStatus.PROPOSED))
    app = _app(store)
    captured = _capture(monkeypatch)
    response = await _post(app, {"workspace_id": "1", "plan_execution_id": "plan_1", "expected_plan_revision": 1, "message": "execute"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "client_refresh_required"
    assert not captured
    assert not store.claimed
